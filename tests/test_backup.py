"""
Sandboxed test of btrfs_restore.backup - no real btrfs, no root.

A FakeBtrfsOps stands in for the `btrfs`/`zstd`/`ssh` command layer: snapshots
and received subvolumes are plain directories, "send" copies trees, disk usage
and failure points are configurable.
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from btrfs_restore import config as cfgmod
from btrfs_restore.config import Config, BACKUP_DIRNAME
from btrfs_restore.backup import BtrfsBackupEngine
from btrfs_restore.btrfs_ops import CommandError


class FakeBtrfsOps:
    def __init__(self, *, target_is_btrfs=True, disk_percent=10.0, fail_send_on=None):
        self.target_is_btrfs = target_is_btrfs
        self.disk_percent = disk_percent
        self.fail_send_on = fail_send_on or set()   # kinds whose send should fail
        self.subvolumes = set()
        self.sent = []
        self.ssh_sent = []                          # (kind, parent_name) to remote
        self.remote_subvols = {"root": [], "home": []}
        self.ssh_calls = []

    def snapshot_ro(self, source: Path, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / ".subvol_marker").write_text(str(source))
        self.subvolumes.add(str(dest))

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
        if cmd_argv[:4] == ["sudo", "btrfs", "subvolume", "list"]:
            kind = cmd_argv[-1].rstrip("/").split("/")[-1]
            out = "".join(f"ID 1 gen 1 top level 5 path {kind}/{n}\n"
                          for n in self.remote_subvols.get(kind, []))
        return _CP(0, out, "")

    def send_ssh_receive(self, source, parent, ssh_argv, remote, receive_argv):
        kind = source.name.split("_", 1)[0]
        self.ssh_sent.append((kind, parent.name if parent else None))
        if kind in self.fail_send_on:
            return 1
        self.remote_subvols.setdefault(kind, []).append(source.name)
        return 0

    def push_tree(self, local_dir, ssh_argv, remote, remote_dir):
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

    def test_remote_send_failure_is_partial(self):
        cfg = self._remote_cfg()
        ops = FakeBtrfsOps(target_is_btrfs=True, fail_send_on={"homefs"})
        r = BtrfsBackupEngine(cfg, ops=ops).run()
        self.assertEqual(r.status, "partial", r.message)

    def test_no_target_at_all_fails(self):
        cfg = self.make_cfg()
        cfg.target_root = None
        cfg.remote_host = ""
        r = BtrfsBackupEngine(cfg, ops=FakeBtrfsOps()).run()
        self.assertEqual(r.status, "failed")
        self.assertIn("target", r.message.lower())


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
