"""
#16 parts 2-4: restoring a few files from the SSH host should not stream the
whole subvolume.

- a live byte/rate `pv` stage is spliced into the staging pipeline (part 3);
- a read-only SSHFS mount is tried first, argv-only, and cleaned up on
  failure / cancel / dead-run sweep (part 4).
"""
import os
import subprocess
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from btrfs_restore import config as cfgmod
from btrfs_restore import engine as engmod
from btrfs_restore.config import Config
from btrfs_restore.engine import RestoreEngine
from btrfs_restore.models import SnapshotInfo, SnapshotType


def _snap(**kw):
    d = dict(id="remote_home_x", name="Remote: Home", path=Path("/backups/home/home_x"),
             snap_type=SnapshotType.REMOTE, timestamp=datetime(2026, 9, 9),
             is_subvolume=True)
    d.update(kw)
    return SnapshotInfo(**d)


class _Base(unittest.TestCase):
    def setUp(self):
        self._p = [
            mock.patch.object(cfgmod, "_home_of", return_value=Path("/nonexistent")),
            mock.patch.object(cfgmod, "_autodetect_backup_target", return_value=None),
            mock.patch.dict("os.environ", {"USER": "bob"}, clear=True),
        ]
        for p in self._p:
            p.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.eng = RestoreEngine()
        self.eng._config.staging_dir = Path(self.tmp.name) / "staging"
        self.eng.staging_dir = self.eng._config.staging_dir

    def tearDown(self):
        for p in self._p:
            p.stop()
        self.tmp.cleanup()

    def _cfg(self, **kw):
        c = Config()
        c.remote_host = kw.get("host", "10.0.0.9")
        c.remote_user = kw.get("user", "bob")
        c.remote_port = kw.get("port", 22)
        return c


class TestPvSplice(_Base):
    def test_no_pv_stage_without_a_progress_callback(self):
        self.eng._progress_cb = None
        stages, idx = self.eng._maybe_pv([["a"], ["b", "c"]])
        self.assertEqual(idx, -1)
        self.assertEqual(stages, [["a"], ["b", "c"]])

    def test_pv_spliced_before_the_final_stage(self):
        self.eng._progress_cb = lambda _t: None
        with mock.patch.object(engmod.shutil, "which", return_value="/usr/bin/pv"):
            stages, idx = self.eng._maybe_pv(
                [["ssh", "host", "btrfs", "send", "/s"], ["btrfs", "receive", "/d"]])
        self.assertEqual(stages[idx][0], "pv")
        self.assertEqual(stages[-1], ["btrfs", "receive", "/d"])
        self.assertEqual(len(stages), 3)

    def test_no_pv_stage_when_pv_is_missing(self):
        self.eng._progress_cb = lambda _t: None
        with mock.patch.object(engmod.shutil, "which", return_value=None):
            _stages, idx = self.eng._maybe_pv([["a"], ["b"]])
        self.assertEqual(idx, -1)

    def test_progress_pipeline_reports_bytes(self):
        seen = []
        self.eng._progress_cb = seen.append
        # printf -> pv -> cat : pv writes a byte/rate line to its stderr
        self.eng._run_pipeline([["printf", "hello world"], ["cat"]], progress=True)
        self.assertTrue(any("B" in line for line in seen),
                        f"no pv byte readout seen: {seen}")


class TestRemoteMount(_Base):
    def test_returns_none_without_sshfs(self):
        with mock.patch.object(engmod.shutil, "which", return_value=None):
            self.assertIsNone(self.eng.mount_remote_snapshot(_snap()))

    def test_returns_none_for_a_local_snapshot(self):
        with mock.patch.object(engmod.shutil, "which", return_value="/usr/bin/sshfs"):
            self.assertIsNone(
                self.eng.mount_remote_snapshot(_snap(snap_type=SnapshotType.LOCAL)))

    def test_sshfs_argv_is_argv_only_and_read_only(self):
        argv = self.eng._sshfs_argv(_snap(), self._cfg(port=2222), "bob", Path("/mnt/x"))
        self.assertEqual(argv[0], "sshfs")
        self.assertEqual(argv[1], "bob@10.0.0.9:/backups/home/home_x")  # one element
        self.assertIn("ro", argv)
        self.assertIn("2222", argv)
        # a shell-hostile path stays a single argument
        evil = self.eng._sshfs_argv(_snap(path=Path('/b/x"; rm -rf / #')),
                                    self._cfg(), "bob", Path("/mnt/x"))
        self.assertTrue(any('rm -rf' in a for a in evil))
        self.assertEqual(sum('rm -rf' in a for a in evil), 1)

    def test_failed_mount_leaves_no_mountpoint_and_no_tracked_mount(self):
        self.eng._config.remote_host = "10.0.0.9"
        with mock.patch.object(engmod.shutil, "which", return_value="/usr/bin/sshfs"), \
             mock.patch.object(engmod, "Config") as C, \
             mock.patch.object(engmod.subprocess, "run") as run, \
             mock.patch("os.path.ismount", return_value=False):
            C.load.return_value = self._cfg()
            run.return_value = subprocess.CompletedProcess([], 1, "", "connect refused")
            out = self.eng.mount_remote_snapshot(_snap())
        self.assertIsNone(out)
        self.assertEqual(self.eng._remote_mounts, [])
        base = self.eng._remote_mnt_base()
        self.assertFalse(base.exists() and any(base.iterdir()))

    def test_successful_mount_is_tracked_and_cancel_unmounts_it(self):
        self.eng._config.remote_host = "10.0.0.9"
        unmounted = []
        with mock.patch.object(engmod.shutil, "which", return_value="/usr/bin/sshfs"), \
             mock.patch.object(engmod, "Config") as C, \
             mock.patch.object(engmod.subprocess, "run") as run, \
             mock.patch("os.path.ismount", return_value=True):
            C.load.return_value = self._cfg()
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            out = self.eng.mount_remote_snapshot(_snap())
        self.assertIsNotNone(out)
        self.assertEqual(len(self.eng._remote_mounts), 1)
        tracked = self.eng._remote_mounts[0]

        with mock.patch.object(self.eng, "_unmount_quiet",
                               side_effect=lambda p, **k: unmounted.append(p)):
            self.eng.cancel_active_operation()
        self.assertIn(tracked, unmounted)
        self.assertEqual(self.eng._remote_mounts, [])

    def test_sweep_removes_dead_run_mount_keeps_live_one(self):
        base = self.eng._remote_mnt_base()
        base.mkdir(parents=True)
        live = base / f"{os.getpid()}-home_x"
        dead = base / "999999999-home_y"
        live.mkdir()
        dead.mkdir()
        with mock.patch.object(self.eng, "_unmount_quiet") as um:
            self.eng._sweep_orphan_mounts()
        self.assertTrue(live.exists())
        self.assertFalse(dead.exists())
        um.assert_called_once()


if __name__ == "__main__":
    unittest.main()
