"""
Sandboxed test of btrfs_restore.backup - no real btrfs, no root.

A FakeBtrfsOps stands in for the `btrfs`/`zstd`/`ssh` command layer: snapshots
and received subvolumes are plain directories, "send" copies trees, disk usage
and failure points are configurable.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from btrfs_restore import config as cfgmod
from btrfs_restore.config import Config, BACKUP_DIRNAME
from btrfs_restore.backup import BtrfsBackupEngine
from btrfs_restore.btrfs_ops import CommandError, prune_paths


class FakeBtrfsOps:
    def __init__(self, *, target_is_btrfs=True, disk_percent=10.0, fail_send_on=None,
                 list_failure=None, missing_dir=False):
        self.missing_dir = missing_dir              # remote <base>/<kind> not created yet
        self.list_failure = list_failure            # None | "timeout" | "ssh255"
        self.target_is_btrfs = target_is_btrfs
        self.disk_percent = disk_percent
        self.fail_send_on = fail_send_on or set()   # kinds whose send should fail
        self.subvolumes = set()
        self.sent = []
        self.ssh_sent = []                          # (kind, parent_name) to remote
        self.remote_subvols = {"root": [], "home": []}
        self.remote_partial = {}                    # kind -> half-received (rw) names
        self.remote_children = set()                # partial names that have a dependent snapshot
        self.receive_alive = False                  # a btrfs receive is running remotely
        self.ssh_deleted = []
        self.ssh_calls = []
        self.pushed_trees = []                       # [(remote_dir, {relpath: bytes})]

    def snapshot_ro(self, source: Path, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, dest, dirs_exist_ok=True)
        (dest / ".subvol_marker").write_text(str(source))
        self.subvolumes.add(str(dest))

    def snapshot_rw(self, source: Path, dest: Path) -> None:
        self.snapshot_ro(source, dest)

    def set_readonly(self, path: Path, value: bool = True) -> None:
        pass

    def prune_paths(self, root: Path, patterns) -> list:
        return prune_paths(Path(root), patterns, self.is_subvolume)

    def delete_subvolume(self, path: Path) -> None:
        self.subvolumes.discard(str(path))
        shutil.rmtree(path, ignore_errors=True)

    def is_subvolume(self, path: Path) -> bool:
        return str(path) in self.subvolumes

    def is_btrfs(self, path: Path) -> bool:
        return self.target_is_btrfs

    def send_local_receive(self, source, parent, dest_dir) -> int:
        kind = source.name.split("_", 1)[0]
        self.sent.append((kind, parent.name if parent else None))
        if kind in self.fail_send_on:
            return 1
        received = dest_dir / source.name
        shutil.copytree(source, received)
        self.subvolumes.add(str(received))
        return 0

    def send_to_stream(self, source, parent, out_file, level=3) -> int:
        kind = source.name.split("_", 1)[0]
        self.sent.append((kind, parent.name if parent else None))
        if kind in self.fail_send_on:
            return 1
        out_file.write_bytes(b"FAKE-BTRFS-STREAM")
        return 0

    def disk_usage_percent(self, path: Path) -> float:
        return self.disk_percent

    # -- remote --------------------------------------------------
    def ssh_capture(self, ssh_argv, remote, cmd_argv, timeout=25):
        self.ssh_calls.append(list(cmd_argv))
        out = ""
        if cmd_argv[0] == "pgrep":
            return _CP(0 if self.receive_alive else 1, "", "")
        if cmd_argv[:4] == ["sudo", "btrfs", "subvolume", "delete"]:
            self.ssh_deleted.append(cmd_argv[-1])
            return _CP(0, "", "")
        if cmd_argv[:2] == ["test", "-d"] and self.missing_dir:
            return _CP(1, "", "")
        if cmd_argv[:4] == ["sudo", "btrfs", "subvolume", "list"]:
            if self.list_failure == "timeout":
                raise subprocess.TimeoutExpired(cmd_argv, timeout)
            if self.list_failure == "ssh255":
                return _CP(255, "", "ssh: connect to host 10.0.0.9: No route to host")
            kind = cmd_argv[-1].rstrip("/").split("/")[-1]
            out = "".join(f"ID 1 gen 1 top level 5 parent_uuid - received_uuid u-{n} "
                          f"uuid id-{n} path {kind}/{n}\n"
                          for n in self.remote_subvols.get(kind, []))
            if "-r" not in cmd_argv:       # -r = read-only only: partials show up otherwise
                for n in self.remote_partial.get(kind, []):
                    out += (f"ID 2 gen 2 top level 5 parent_uuid - received_uuid - "
                            f"uuid id-{n} path {kind}/{n}\n")
        if cmd_argv[:4] == ["sudo", "btrfs", "subvolume", "list"]:
            for n in self.remote_partial.get(kind, []):
                if n in self.remote_children:      # a complete snapshot depending on it
                    out += (f"ID 3 gen 3 top level 5 parent_uuid id-{n} received_uuid u-kid "
                            f"uuid id-kid path {kind}/{kind}_20991231_000000\n")
        return _CP(0, out, "")

    def send_ssh_receive(self, source, parent, ssh_argv, remote, receive_argv):
        kind = source.name.split("_", 1)[0]
        self.ssh_sent.append((kind, parent.name if parent else None))
        if kind in self.fail_send_on:
            return 1
        self.remote_subvols.setdefault(kind, []).append(source.name)
        return 0

    def push_tree(self, local_dir, ssh_argv, remote, remote_dir, timeout=120):
        # snapshot the pushed tree (file names -> text) so tests can inspect it
        from pathlib import Path as _P
        pushed = {}
        for p in _P(local_dir).rglob("*"):
            if p.is_file():
                try:
                    pushed[str(p.relative_to(local_dir))] = p.read_bytes()
                except OSError:
                    pass
        self.pushed_trees.append((remote_dir, pushed))
        return 0, ""


class _CP:
    def __init__(self, returncode, stdout, stderr):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class StubStateCollector:
    """No-op stand-in for SystemStateCollector - no shelling out to pacman."""
    def __init__(self, snapshot_dir):
        self.d = snapshot_dir

    def collect_all(self):
        meta = self.d / "_system_state"
        meta.mkdir(parents=True, exist_ok=True)
        (meta / "os_info.json").write_text('{"user": "tester"}')
        (self.d / "restore.sh").write_text("#!/usr/bin/env bash\ntrue\n")
        return []


class BackupTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.fake_root_src = root / "rootfs"
        self.fake_home_src = root / "homefs"
        self.local_snaps = root / "dot_snapshots"
        self.target = root / "usb"
        for p in (self.fake_root_src, self.fake_home_src, self.local_snaps, self.target):
            p.mkdir(parents=True)
        (self.fake_home_src / "Documents").mkdir()
        (self.fake_home_src / "Documents" / "note.md").write_text("hello")

        self._patchers = [
            mock.patch.object(cfgmod, "_home_of", return_value=root),
            mock.patch.object(cfgmod, "_autodetect_backup_target", return_value=None),
            mock.patch("btrfs_restore.backup.os.geteuid", return_value=0),
            # the fake USB dir here is a plain tempdir, not a real mountpoint -
            # tests aren't exercising the mount-point safety check itself.
            mock.patch("btrfs_restore.backup.os.path.ismount", return_value=True),
            mock.patch("btrfs_restore.backup.SystemStateCollector", StubStateCollector),
            mock.patch.dict(os.environ, {}, clear=True),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self.tmp.cleanup()

    def make_cfg(self) -> Config:
        cfg = Config()
        cfg.source_mounts = [str(self.fake_root_src), str(self.fake_home_src)]
        cfg.local_snapshots_dir = self.local_snaps
        cfg.target_root = self.target / BACKUP_DIRNAME
        cfg.max_disk_percent = 80
        cfg.min_keep = 2
        return cfg

    def kinds_of(self, cfg):
        # engine derives: "/" -> root, else Path(mount).name ; here names are src_*
        return [Path(m).name for m in cfg.source_mounts]


class TestBackupHappyPath(BackupTestBase):
    def test_full_then_incremental(self):
        cfg = self.make_cfg()
        ops = FakeBtrfsOps(target_is_btrfs=True)

        r1 = BtrfsBackupEngine(cfg, ops=ops).run()
        self.assertEqual(r1.status, "completed", r1.message)
        snap1 = cfg.snapshots_dir / r1.snapshot_name
        man1 = json.loads((snap1 / "manifest.json").read_text())
        self.assertEqual(man1["status"], "completed")
        self.assertTrue((snap1 / "_system_state" / "os_info.json").exists())
        self.assertTrue((snap1 / "restore.sh").exists())
        # latest -> this snapshot
        self.assertTrue(cfg.latest_link.is_symlink())
        self.assertEqual(os.path.basename(os.readlink(cfg.latest_link)), r1.snapshot_name)
        # first run: every send was full
        self.assertTrue(all(parent is None for _, parent in ops.sent))

        r2 = BtrfsBackupEngine(cfg, ops=ops).run()
        self.assertEqual(r2.status, "completed", r2.message)
        # second run: parents point at run 1's local snapshots
        self.assertTrue(all(parent is not None for _, parent in ops.sent[len(self.kinds_of(cfg)):]))
        self.assertEqual(os.path.basename(os.readlink(cfg.latest_link)), r2.snapshot_name)
        self.assertNotEqual(r1.snapshot_name, r2.snapshot_name)

    def test_stream_target_when_not_btrfs(self):
        cfg = self.make_cfg()
        ops = FakeBtrfsOps(target_is_btrfs=False)
        r = BtrfsBackupEngine(cfg, ops=ops).run()
        self.assertEqual(r.status, "completed", r.message)
        snap = cfg.snapshots_dir / r.snapshot_name
        streams = list(snap.glob("*.btrfs.zst"))
        self.assertEqual(len(streams), 2)


class TestBackupSafety(BackupTestBase):
    def test_partial_run_does_not_move_latest_or_become_parent(self):
        cfg = self.make_cfg()
        ok = FakeBtrfsOps(target_is_btrfs=True)
        r1 = BtrfsBackupEngine(cfg, ops=ok).run()
        good_latest = os.readlink(cfg.latest_link)

        bad = FakeBtrfsOps(target_is_btrfs=True, fail_send_on={"homefs"})
        r2 = BtrfsBackupEngine(cfg, ops=bad).run()
        self.assertEqual(r2.status, "partial", r2.message)
        # latest still points at the good snapshot
        self.assertEqual(os.readlink(cfg.latest_link), good_latest)
        # the failed snapshot dir is kept, marked partial
        bad_dir = cfg.snapshots_dir / r2.snapshot_name
        self.assertEqual(json.loads((bad_dir / "manifest.json").read_text())["status"], "partial")

        # next good run parents off run 1's local snapshots, not the partial
        man1 = json.loads((cfg.snapshots_dir / r1.snapshot_name / "manifest.json").read_text())
        good2 = FakeBtrfsOps(target_is_btrfs=True)
        r3 = BtrfsBackupEngine(cfg, ops=good2).run()
        self.assertEqual(r3.status, "completed")
        self.assertEqual({p for _, p in good2.sent}, set(man1["local_snapshots"].values()))

    def test_no_target(self):
        cfg = self.make_cfg()
        cfg.target_root = None
        r = BtrfsBackupEngine(cfg, ops=FakeBtrfsOps()).run()
        self.assertEqual(r.status, "failed")

    def test_needs_root(self):
        cfg = self.make_cfg()
        with mock.patch("btrfs_restore.backup.os.geteuid", return_value=1000):
            r = BtrfsBackupEngine(cfg, ops=FakeBtrfsOps()).run()
        self.assertEqual(r.status, "failed")
        self.assertIn("root", r.message)

    def test_stale_unmounted_target_dir_is_rejected_not_written_to(self):
        """Real incident: TARGET_DIR pinned to a USB mount point that was
        left behind (drive unplugged) is still a perfectly writable plain
        directory - it must NOT be accepted as a live backup target, or the
        stream lands on / instead of the USB and nobody notices."""
        cfg = self.make_cfg()
        # this test's own fixture patches os.path.ismount -> True for
        # everything (see BackupTestBase.setUp); here we want the real,
        # narrower answer for this one specific "stale" path.
        with mock.patch("btrfs_restore.backup.os.path.ismount", return_value=False):
            r = BtrfsBackupEngine(cfg, ops=FakeBtrfsOps(target_is_btrfs=True)).run()
        self.assertEqual(r.status, "failed")
        self.assertIn("No backup target", r.message)
        if cfg.target_root.parent.exists():
            self.assertFalse(any(cfg.target_root.parent.iterdir()),
                              "nothing should have been written to the fake target")


class TestBackupRetention(BackupTestBase):
    def test_disk_pressure_prunes_oldest_completed_to_min_keep(self):
        cfg = self.make_cfg()
        cfg.min_keep = 2
        names = []
        # 4 completed snapshots, drive "full" -> should prune down to min_keep
        for i in range(4):
            ops = FakeBtrfsOps(target_is_btrfs=True,
                               disk_percent=95.0 if i == 3 else 10.0)
            with mock.patch("btrfs_restore.backup.datetime") as dt:
                dt.now.return_value = _fixed_dt(i)
                r = BtrfsBackupEngine(cfg, ops=ops).run()
            self.assertEqual(r.status, "completed", r.message)
            names.append(r.snapshot_name)

        left = sorted(s.name for s in cfg.snapshots_dir.iterdir() if s.is_dir())
        self.assertEqual(len(left), 2)
        self.assertEqual(left, sorted(names[-2:]))

    def test_local_snapshots_kept_generously_and_parent_never_pruned(self):
        cfg = self.make_cfg()
        cfg.local_keep = 2
        for i in range(5):
            with mock.patch("btrfs_restore.backup.datetime") as dt:
                dt.now.return_value = _fixed_dt(i)
                r = BtrfsBackupEngine(cfg, ops=FakeBtrfsOps(target_is_btrfs=True)).run()
                self.assertEqual(r.status, "completed", r.message)
        per_kind = {}
        for e in self.local_snaps.iterdir():
            if e.is_dir():
                per_kind.setdefault(e.name.split("_", 1)[0], []).append(e.name)
        # keep_n = max(2, local_keep) -> 2 per kind
        for kind, names in per_kind.items():
            self.assertLessEqual(len(names), 3, names)  # 2 kept + possibly the recorded parent
        # the parent recorded by the latest completed backup still exists
        man = BtrfsBackupEngine(cfg).latest_completed_manifest()
        for name in man["local_snapshots"].values():
            self.assertTrue((self.local_snaps / name).is_dir())

    def test_hard_count_cap(self):
        cfg = self.make_cfg()
        cfg.max_snapshots = 2
        for i in range(4):
            with mock.patch("btrfs_restore.backup.datetime") as dt:
                dt.now.return_value = _fixed_dt(i)
                BtrfsBackupEngine(cfg, ops=FakeBtrfsOps(target_is_btrfs=True)).run()
        left = [s for s in cfg.snapshots_dir.iterdir() if s.is_dir()]
        self.assertEqual(len(left), 2)


class TestBackupRemote(BackupTestBase):
    def _remote_cfg(self, with_usb=False):
        cfg = self.make_cfg()
        if not with_usb:
            cfg.target_root = None
        cfg.remote_host = "10.0.0.9"
        cfg.remote_path = "/srv/backups"
        cfg.remote_user = "bob"
        return cfg

    def test_remote_only_backup_works_without_usb(self):
        cfg = self._remote_cfg()
        ops = FakeBtrfsOps(target_is_btrfs=True)
        r = BtrfsBackupEngine(cfg, ops=ops).run()
        self.assertEqual(r.status, "completed", r.message)
        # first run: full sends of root + home to the remote
        self.assertEqual(sorted(k for k, _ in ops.ssh_sent), ["homefs", "rootfs"])
        self.assertTrue(all(p is None for _, p in ops.ssh_sent))

    def test_remote_incremental_uses_local_parent(self):
        cfg = self._remote_cfg()
        # pretend a previous run left rootfs_OLD/homefs_OLD both locally and remote
        for kind in ("rootfs", "homefs"):
            (self.local_snaps / f"{kind}_20260101_000000").mkdir()
        ops = FakeBtrfsOps(target_is_btrfs=True)
        ops.remote_subvols = {"rootfs": ["rootfs_20260101_000000"],
                              "homefs": ["homefs_20260101_000000"]}
        r = BtrfsBackupEngine(cfg, ops=ops).run()
        self.assertEqual(r.status, "completed", r.message)
        self.assertTrue(all(p == f"{k}_20260101_000000" for k, p in ops.ssh_sent))

    def test_query_timeout_does_not_fall_back_to_full(self):
        cfg = self._remote_cfg()
        ops = FakeBtrfsOps(target_is_btrfs=True, list_failure="timeout")
        r = BtrfsBackupEngine(cfg, ops=ops).run()
        self.assertEqual(r.status, "failed", r.message)
        self.assertEqual(ops.ssh_sent, [])          # nothing sent, no blind full
        self.assertIn("timed out", r.message)

    def test_query_ssh_error_does_not_fall_back_to_full(self):
        cfg = self._remote_cfg()
        ops = FakeBtrfsOps(target_is_btrfs=True, list_failure="ssh255")
        r = BtrfsBackupEngine(cfg, ops=ops).run()
        self.assertEqual(r.status, "failed", r.message)
        self.assertEqual(ops.ssh_sent, [])
        self.assertIn("No route to host", r.message)

    def test_answered_but_empty_remote_still_does_full(self):
        cfg = self._remote_cfg()
        ops = FakeBtrfsOps(target_is_btrfs=True)    # list ok, holds nothing
        r = BtrfsBackupEngine(cfg, ops=ops).run()
        self.assertEqual(r.status, "completed", r.message)
        self.assertTrue(all(p is None for _, p in ops.ssh_sent))
        self.assertEqual(len(ops.ssh_sent), 2)

    def test_remote_full_kinds_reports_full_when_remote_empty(self):
        cfg = self._remote_cfg()
        eng = BtrfsBackupEngine(cfg, ops=FakeBtrfsOps(target_is_btrfs=True))
        self.assertEqual(len(eng.remote_full_kinds()), len(cfg.source_mounts))

    def test_remote_full_kinds_empty_when_incremental_possible(self):
        cfg = self._remote_cfg()
        ops = FakeBtrfsOps(target_is_btrfs=True)
        for m in cfg.source_mounts:
            kind = "root" if m == "/" else Path(m).name
            (self.local_snaps / f"{kind}_20260101_000000").mkdir()
            ops.remote_subvols[kind] = [f"{kind}_20260101_000000"]
        self.assertEqual(BtrfsBackupEngine(cfg, ops=ops).remote_full_kinds(), [])

    def test_remote_full_kinds_raises_when_remote_cannot_be_asked(self):
        from btrfs_restore.backup import RemoteQueryError
        cfg = self._remote_cfg()
        eng = BtrfsBackupEngine(cfg, ops=FakeBtrfsOps(list_failure="timeout"))
        with self.assertRaises(RemoteQueryError):
            eng.remote_full_kinds()

    def test_missing_remote_folder_counts_as_no_base_not_as_error(self):
        cfg = self._remote_cfg()
        eng = BtrfsBackupEngine(cfg, ops=FakeBtrfsOps(missing_dir=True))
        self.assertEqual(len(eng.remote_full_kinds()), len(cfg.source_mounts))

    def test_skip_remote_sends_nothing(self):
        cfg = self._remote_cfg(with_usb=True)
        ops = FakeBtrfsOps(target_is_btrfs=True)
        eng = BtrfsBackupEngine(cfg, ops=ops)
        eng.skip_remote = True
        r = eng.run()
        self.assertEqual(r.status, "completed", r.message)
        self.assertEqual(ops.ssh_sent, [])

    def _local_and_remote(self, ops, kind, name, partial=False):
        (self.local_snaps / name).mkdir(exist_ok=True)
        target = ops.remote_partial if partial else ops.remote_subvols
        target.setdefault(kind, []).append(name)

    def test_half_received_remote_snapshot_is_never_the_base(self):
        cfg = self._remote_cfg()
        ops = FakeBtrfsOps(target_is_btrfs=True)
        for kind in ("rootfs", "homefs"):
            self._local_and_remote(ops, kind, f"{kind}_20260101_000000")
            self._local_and_remote(ops, kind, f"{kind}_20260102_000000", partial=True)
        r = BtrfsBackupEngine(cfg, ops=ops).run()
        self.assertEqual(r.status, "completed", r.message)
        # falls back to the older COMPLETE copy, not the newer broken one
        self.assertTrue(all(p == f"{k}_20260101_000000" for k, p in ops.ssh_sent))

    def test_only_half_received_copies_means_full(self):
        cfg = self._remote_cfg()
        ops = FakeBtrfsOps(target_is_btrfs=True)
        for kind in ("rootfs", "homefs"):
            self._local_and_remote(ops, kind, f"{kind}_20260102_000000", partial=True)
        eng = BtrfsBackupEngine(cfg, ops=ops)
        self.assertEqual(len(eng.remote_full_kinds()), len(cfg.source_mounts))

    def test_received_uuid_dash_line_is_not_a_valid_base(self):
        self.assertFalse(BtrfsBackupEngine._has_received_uuid(
            "ID 5 gen 1 top level 5 received_uuid - path home/home_20260101_000000"))
        self.assertTrue(BtrfsBackupEngine._has_received_uuid(
            "ID 5 gen 1 top level 5 received_uuid abc-1 path home/home_20260101_000000"))
        self.assertFalse(BtrfsBackupEngine._has_received_uuid("ID 5 path home/x"))

    # -- self-heal of half-received copies (issue #24) --------------------
    def _heal_setup(self, partial_names=None, **fake_kw):
        cfg = self._remote_cfg()
        ops = FakeBtrfsOps(target_is_btrfs=True)
        for key, val in fake_kw.items():
            setattr(ops, key, val)
        self.kinds = ["root" if m == "/" else Path(m).name for m in cfg.source_mounts]
        for k in self.kinds:
            for n in (partial_names or [f"{k}_20260102_000000"]):
                ops.remote_partial.setdefault(k, []).append(n)
        return cfg, ops

    def _run_heal(self, cfg, ops, approved):
        events = []
        eng = BtrfsBackupEngine(cfg, ops=ops, callback=lambda t, m: events.append((t, m)))
        eng.heal_approved = approved
        r = eng.run()
        return r, events

    def test_partial_copies_are_listed_without_deleting(self):
        cfg, ops = self._heal_setup()
        found = BtrfsBackupEngine(cfg, ops=ops).remote_partial_copies()
        self.assertEqual({(f["kind"], f["name"]) for f in found},
                         {(k, f"{k}_20260102_000000") for k in self.kinds})
        self.assertTrue(all(not f["has_children"] for f in found))
        self.assertEqual(ops.ssh_deleted, [])

    def test_name_outside_the_timestamp_pattern_is_never_listed(self):
        cfg, ops = self._heal_setup(partial_names=["manual_thing", "x_20260102_000000"])
        self.assertEqual(BtrfsBackupEngine(cfg, ops=ops).remote_partial_copies(), [])

    def test_approved_partial_copy_is_deleted_and_logged(self):
        cfg, ops = self._heal_setup()
        approved = [(k, f"{k}_20260102_000000") for k in self.kinds]
        r, events = self._run_heal(cfg, ops, approved)
        self.assertEqual(r.status, "completed", r.message)
        self.assertEqual(sorted(ops.ssh_deleted), sorted(
            f"/srv/backups/{k}/{k}_20260102_000000" for k in self.kinds))
        self.assertTrue(any("deleted partial copy" in m for _, m in events))

    def test_nothing_deleted_without_approval(self):
        cfg, ops = self._heal_setup()
        self._run_heal(cfg, ops, [])
        self.assertEqual(ops.ssh_deleted, [])

    def test_nothing_deleted_while_a_receive_is_running(self):
        cfg, ops = self._heal_setup(receive_alive=True)
        approved = [(k, f"{k}_20260102_000000") for k in self.kinds]
        _, events = self._run_heal(cfg, ops, approved)
        self.assertEqual(ops.ssh_deleted, [])
        self.assertTrue(any("receive is running" in m for _, m in events))

    def test_copy_with_dependents_is_kept(self):
        cfg, ops = self._heal_setup()
        for k in self.kinds:
            ops.remote_children.add(f"{k}_20260102_000000")
        found = BtrfsBackupEngine(cfg, ops=ops).remote_partial_copies()
        self.assertTrue(all(f["has_children"] for f in found))
        approved = [(k, f"{k}_20260102_000000") for k in self.kinds]
        _, events = self._run_heal(cfg, ops, approved)
        self.assertEqual(ops.ssh_deleted, [])
        self.assertTrue(any("depending on it" in m for _, m in events))

    def test_approved_name_that_is_not_partial_is_not_deleted(self):
        cfg, ops = self._heal_setup()
        good = {}
        for k in self.kinds:
            self._local_and_remote(ops, k, f"{k}_20260101_000000")
            good[k] = f"{k}_20260101_000000"
        _, events = self._run_heal(cfg, ops, list(good.items()))
        self.assertFalse([d for d in ops.ssh_deleted if "20260101" in d])

    def test_query_failure_while_healing_deletes_nothing(self):
        cfg, ops = self._heal_setup(list_failure="ssh255")
        approved = [(k, f"{k}_20260102_000000") for k in self.kinds]
        self._run_heal(cfg, ops, approved)
        self.assertEqual(ops.ssh_deleted, [])

    def test_receiver_error_reaches_the_log_and_the_summary(self):
        cfg = self._remote_cfg()
        ops = FakeBtrfsOps(target_is_btrfs=True, fail_send_on={"homefs"})
        ops.last_stderr = "ssh[1]: ERROR: cannot receive: parent subvol is not read-only"
        events = []
        r = BtrfsBackupEngine(cfg, ops=ops, callback=lambda t, m: events.append((t, m))).run()
        self.assertEqual(r.status, "partial", r.message)
        self.assertIn("parent subvol is not read-only", r.message)
        self.assertTrue(any("parent subvol is not read-only" in m for t, m in events
                            if t == "error"))

    def test_remote_prune_ignores_names_outside_the_timestamp_pattern(self):
        cfg = self._remote_cfg()
        cfg.local_keep = 2
        ops = FakeBtrfsOps(target_is_btrfs=True)
        for k in ("root", "home"):      # _prune_remote walks the real kinds
            ops.remote_subvols[k] = [f"{k}_2026010{i}_000000" for i in range(1, 5)] + [f"{k}_manual"]
        r = BtrfsBackupEngine(cfg, ops=ops).run()
        self.assertEqual(r.status, "completed", r.message)
        self.assertFalse([d for d in ops.ssh_deleted if d.endswith("_manual")])
        self.assertTrue([d for d in ops.ssh_deleted if "_20260101_" in d])   # old ones still go
        # the manual name must not push a real copy out of the keep window:
        # the two newest TIMESTAMPED copies (03 and 04) stay
        self.assertFalse([d for d in ops.ssh_deleted if "_20260103_" in d or "_20260104_" in d])

    def test_remote_send_failure_is_partial(self):
        cfg = self._remote_cfg()
        ops = FakeBtrfsOps(target_is_btrfs=True, fail_send_on={"homefs"})
        r = BtrfsBackupEngine(cfg, ops=ops).run()
        self.assertEqual(r.status, "partial", r.message)

    def test_completed_remote_backup_pushes_a_recovery_kit(self):
        cfg = self._remote_cfg()
        ops = FakeBtrfsOps(target_is_btrfs=True)
        r = BtrfsBackupEngine(cfg, ops=ops).run()
        self.assertEqual(r.status, "completed", r.message)

        kit = next((files for dest, files in ops.pushed_trees
                    if dest.rstrip("/") == "/srv/backups"), None)
        self.assertIsNotNone(kit, "recovery kit was not pushed to <base>/")
        self.assertIn("disaster-recovery.sh", kit)
        self.assertIn("RECOVERY.md", kit)
        self.assertIn("btrfs-restore-tui-src.tar.gz", kit)
        script = kit["disaster-recovery.sh"].decode()
        self.assertIn("bob@10.0.0.9", script)          # placeholders filled in
        self.assertIn("/srv/backups", script)
        self.assertNotIn("@@", script)
        import subprocess as sp, tempfile, os as _os
        with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
            fh.write(script)
            path = fh.name
        try:
            self.assertEqual(sp.run(["bash", "-n", path]).returncode, 0)
        finally:
            _os.unlink(path)

    def test_state_dir_still_has_content_for_remote_after_usb_runs_first(self):
        """Real-hardware finding: with USB + remote both configured, USB used
        to MOVE the shared state_dir's contents into its own snapshot dir,
        leaving nothing for the remote push that runs right after - meta/<name>/
        landed empty with no error (push_tree of an empty dir "succeeds")."""
        cfg = self._remote_cfg(with_usb=True)
        ops = FakeBtrfsOps(target_is_btrfs=True)
        r = BtrfsBackupEngine(cfg, ops=ops).run()
        self.assertEqual(r.status, "completed", r.message)

        # USB snapshot still got its own copy of the state
        snap = cfg.snapshots_dir / r.snapshot_name
        self.assertTrue((snap / "_system_state" / "os_info.json").exists())

        # AND the remote meta/<name>/ push actually carried files, not an
        # empty tar
        meta_push = next((files for dest, files in ops.pushed_trees
                          if dest.rstrip("/").endswith(f"meta/{r.snapshot_name}")), None)
        self.assertIsNotNone(meta_push, "no push to meta/<name>/ was recorded")
        self.assertIn("_system_state/os_info.json", meta_push)

    def test_no_target_at_all_fails(self):
        cfg = self.make_cfg()
        cfg.target_root = None
        cfg.remote_host = ""
        r = BtrfsBackupEngine(cfg, ops=FakeBtrfsOps()).run()
        self.assertEqual(r.status, "failed")
        self.assertIn("target", r.message.lower())


class TestBackupExclusions(BackupTestBase):
    def _seed_caches(self):
        # a user home with a fat cache + trash that must not be backed up
        u = self.fake_home_src / "hbarchini"
        (u / ".cache" / "mozilla").mkdir(parents=True)
        (u / ".cache" / "mozilla" / "blob").write_bytes(b"x" * 4096)
        (u / ".local" / "share" / "Trash" / "files").mkdir(parents=True)
        (u / ".local" / "share" / "Trash" / "files" / "junk").write_text("junk")
        (u / ".config" / "app").mkdir(parents=True)
        (u / ".config" / "app" / "settings.json").write_text("{}")

    def test_caches_are_left_out_and_recorded(self):
        self._seed_caches()
        cfg = self.make_cfg()
        # engine derives kind from Path(mount).name -> "homefs" here
        cfg.extra_exclusions = ["*/.cache", "*/.local/share/Trash"]
        cfg.exclude_defaults = False
        ops = FakeBtrfsOps(target_is_btrfs=True)
        r = BtrfsBackupEngine(cfg, ops=ops).run()
        self.assertEqual(r.status, "completed", r.message)

        local_home = next(p for p in self.local_snaps.iterdir()
                          if p.name.startswith("homefs_"))
        self.assertFalse((local_home / "hbarchini" / ".cache").exists())
        self.assertFalse((local_home / "hbarchini" / ".local" / "share" / "Trash").exists())
        # real content survived
        self.assertTrue((local_home / "hbarchini" / ".config" / "app" / "settings.json").exists())
        self.assertTrue((local_home / "Documents" / "note.md").exists())

        man = json.loads((cfg.snapshots_dir / r.snapshot_name / "manifest.json").read_text())
        self.assertIn("homefs", man["excluded"])
        self.assertIn("hbarchini/.cache", man["excluded"]["homefs"])

    def test_default_list_catches_browser_caches(self):
        u = self.fake_home_src / "hbarchini"
        chrome = u / ".config" / "google-chrome" / "Default"
        (chrome / "Cache" / "Cache_Data").mkdir(parents=True)
        (chrome / "Cache" / "Cache_Data" / "b").write_bytes(b"y" * 8192)
        (chrome / "GPUCache").mkdir(parents=True)
        (chrome / "Service Worker" / "CacheStorage").mkdir(parents=True)
        (chrome / "Preferences").write_text("{}")            # must survive
        (u / ".cache").mkdir()
        code = u / ".config" / "Code"
        (code / "CachedData").mkdir(parents=True)

        cfg = self.make_cfg()
        # sandbox mounts aren't literally "/home", so pull the real default
        # /home list and run it against this realistic tree
        from btrfs_restore.config import DEFAULT_BTRFS_EXCLUSIONS
        cfg.exclude_defaults = False
        cfg.extra_exclusions = list(DEFAULT_BTRFS_EXCLUSIONS["/home"])
        ops = FakeBtrfsOps(target_is_btrfs=True)
        r = BtrfsBackupEngine(cfg, ops=ops).run()
        self.assertEqual(r.status, "completed", r.message)

        h = next(p for p in self.local_snaps.iterdir() if p.name.startswith("homefs_"))
        c = h / "hbarchini" / ".config" / "google-chrome" / "Default"
        self.assertFalse((c / "Cache").exists())
        self.assertFalse((c / "GPUCache").exists())
        self.assertFalse((c / "Service Worker" / "CacheStorage").exists())
        self.assertTrue((c / "Preferences").exists())
        self.assertFalse((h / "hbarchini" / ".config" / "Code" / "CachedData").exists())
        self.assertFalse((h / "hbarchini" / ".cache").exists())

    def test_no_exclusions_uses_plain_ro_snapshot(self):
        self._seed_caches()
        cfg = self.make_cfg()
        cfg.exclude_defaults = False
        cfg.extra_exclusions = []
        ops = FakeBtrfsOps(target_is_btrfs=True)
        r = BtrfsBackupEngine(cfg, ops=ops).run()
        self.assertEqual(r.status, "completed", r.message)
        local_home = next(p for p in self.local_snaps.iterdir()
                          if p.name.startswith("homefs_"))
        self.assertTrue((local_home / "hbarchini" / ".cache").exists())
        man = json.loads((cfg.snapshots_dir / r.snapshot_name / "manifest.json").read_text())
        self.assertEqual(man["excluded"], {})


class TestRecoveryKit(BackupTestBase):
    def test_completed_usb_backup_writes_the_recovery_kit(self):
        cfg = self.make_cfg()
        r = BtrfsBackupEngine(cfg, ops=FakeBtrfsOps(target_is_btrfs=True)).run()
        self.assertEqual(r.status, "completed", r.message)
        root = cfg.target_root
        dr = root / "disaster-recovery.sh"
        self.assertTrue(dr.is_file())
        self.assertTrue(os.access(dr, os.X_OK))
        recovery_md = (root / "RECOVERY.md").read_text()
        self.assertIn("Disaster recovery", recovery_md)
        self.assertIn(r.snapshot_name, recovery_md)
        self.assertTrue((root / "btrfs-restore-tui-src.tar.gz").is_file())
        # the script parses without a shell error
        import subprocess as sp
        self.assertEqual(sp.run(["bash", "-n", str(dr)]).returncode, 0)


class TestRemoteRecoveryKitSnapshotFilter(unittest.TestCase):
    """Real-hardware finding: an old ad-hoc backup script also drops flat files
    into meta/ (dellomar_boot_*.tar.zst, disk_layout_*.txt, pkglist_*.txt,
    *.sfdisk) alongside the real meta/<name>/ directories. The remote
    disaster-recovery.sh's snapshot picker listed those as bogus choices."""

    def test_picker_only_lists_date_named_directories(self):
        from btrfs_restore.backup import _DISASTER_RECOVERY_REMOTE_SH as script
        self.assertIn("-type d", script)  # directories only, not the flat files
        self.assertIn(r"[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{6}", script)  # date-shaped names only
        filled = (script.replace("@@HOST@@", "bob@host")
                        .replace("@@BASE@@", "/srv/backups")
                        .replace("@@PORT@@", "22"))
        r = subprocess.run(["bash", "-n"], input=filled, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class TestBackupScratchCleanup(BackupTestBase):
    def test_run_leaves_no_scratch_dir_and_keeps_last_log(self):
        cfg = self.make_cfg()
        r = BtrfsBackupEngine(cfg, ops=FakeBtrfsOps(target_is_btrfs=True)).run()
        self.assertEqual(r.status, "completed", r.message)
        tmp_root = self.local_snaps / ".backup-tmp"
        leftover = [p for p in tmp_root.iterdir()] if tmp_root.is_dir() else []
        self.assertEqual(leftover, [])
        self.assertTrue((self.local_snaps / ".backup-last.log").is_file())

    def test_sweep_removes_dead_pid_dirs_only(self):
        cfg = self.make_cfg()
        eng = BtrfsBackupEngine(cfg)
        tmp_root = self.local_snaps / ".backup-tmp"
        tmp_root.mkdir(parents=True)
        dead = tmp_root / "999999999-20200101_000000"      # pid can't be alive
        alive = tmp_root / f"{os.getpid()}-20200101_000000"
        for d in (dead, alive):
            d.mkdir()
            (d / "x").write_text("scratch")
        # legacy residue from the pre-1d scheme, aged past the 6h cutoff
        legacy = self.local_snaps / ".state-20200101_000000"
        legacy.mkdir()
        old = _dtmod.datetime(2020, 1, 1).timestamp()
        os.utime(legacy, (old, old))

        eng._sweep_stale_tmp()
        self.assertFalse(dead.exists())
        self.assertTrue(alive.exists())
        self.assertFalse(legacy.exists())


class TestBackupDryRun(BackupTestBase):
    def test_dry_run_writes_nothing(self):
        cfg = self.make_cfg()
        r = BtrfsBackupEngine(cfg, ops=FakeBtrfsOps()).run(dry_run=True)
        self.assertEqual(r.status, "completed")
        self.assertFalse(cfg.snapshots_dir.exists() and any(cfg.snapshots_dir.iterdir()))
        self.assertFalse(any(self.local_snaps.iterdir()))


import datetime as _dtmod


def _fixed_dt(i: int):
    return _dtmod.datetime(2026, 9, 8, 10, i, 0)


if __name__ == "__main__":
    unittest.main()
