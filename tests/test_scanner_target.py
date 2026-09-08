"""
scan_target_snapshots: reads the manifest-based backup-now layout, one entry per
kind, status from the manifest, partial shown / running+failed skipped.
"""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from btrfs_restore import config as cfgmod
from btrfs_restore.config import Config, BACKUP_DIRNAME
from btrfs_restore.scanner import SnapshotScanner
from btrfs_restore.models import SnapshotType


def _make_snapshot(snaps_dir: Path, name: str, status: str, *,
                   streams=False, home_user="hbarchini", created=None):
    d = snaps_dir / name
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({
        "snapshot": name, "status": status,
        "created_at": created or f"{name[:10]}T{name[11:13]}:{name[13:15]}:00",
    }))
    ts = name.replace("-", "").replace("_", "")
    if streams:
        (d / "root.btrfs.zst").write_bytes(b"x")
        (d / "home.btrfs.zst").write_bytes(b"x")
    else:
        (d / f"root_{ts[:8]}_{ts[8:14]}").mkdir()
        home = d / f"home_{ts[:8]}_{ts[8:14]}" / home_user
        home.mkdir(parents=True)
    return d


class TestScanTarget(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.usb = Path(self.tmp.name) / "usb"
        self.snaps = self.usb / BACKUP_DIRNAME / "snapshots"
        self.snaps.mkdir(parents=True)
        self._p = [
            mock.patch.object(cfgmod, "_home_of", return_value=Path(self.tmp.name)),
            mock.patch.object(cfgmod, "_autodetect_backup_target", return_value=str(self.usb)),
            mock.patch.dict("os.environ", {"USER": "hbarchini"}, clear=True),
        ]
        for p in self._p:
            p.start()

    def tearDown(self):
        for p in self._p:
            p.stop()
        self.tmp.cleanup()

    def scanner(self):
        s = SnapshotScanner()
        s.config = Config.load()
        s.user = "hbarchini"
        return s

    def test_completed_snapshot_yields_root_and_home(self):
        _make_snapshot(self.snaps, "2026-09-08_120000", "completed")
        snaps = self.scanner().scan_target_snapshots()
        self.assertEqual(len(snaps), 2)
        kinds = sorted(s.name.split(":")[1].strip().split(" ")[0] for s in snaps)
        self.assertEqual(kinds, ["Home", "Root"])
        for s in snaps:
            self.assertEqual(s.snap_type, SnapshotType.USB)
            self.assertEqual(s.status, "completed")
            self.assertTrue(s.is_subvolume)
        home = next(s for s in snaps if "Home" in s.name)
        self.assertEqual(home.path.name, "hbarchini")   # points into the user dir

    def test_partial_is_listed_and_flagged(self):
        _make_snapshot(self.snaps, "2026-09-08_130000", "partial")
        snaps = self.scanner().scan_target_snapshots()
        self.assertTrue(snaps)
        self.assertTrue(all(s.status == "partial" for s in snaps))
        self.assertIn("(partial)", snaps[0].name)

    def test_running_and_failed_are_skipped(self):
        _make_snapshot(self.snaps, "2026-09-08_140000", "running")
        _make_snapshot(self.snaps, "2026-09-08_150000", "failed")
        _make_snapshot(self.snaps, "2026-09-08_160000", "completed")
        snaps = self.scanner().scan_target_snapshots()
        self.assertEqual({s.name.split("[")[1][:9] for s in snaps}, {"08-Sep 16"})

    def test_stream_snapshot_is_not_a_subvolume(self):
        _make_snapshot(self.snaps, "2026-09-08_170000", "completed", streams=True)
        snaps = self.scanner().scan_target_snapshots()
        self.assertEqual(len(snaps), 2)
        self.assertTrue(all(not s.is_subvolume for s in snaps))
        self.assertTrue(all(s.path.suffix == ".zst" for s in snaps))

    def test_missing_manifest_is_a_warning_not_a_crash(self):
        d = self.snaps / "2026-09-08_180000"
        (d / "root_20260908_180000").mkdir(parents=True)
        with self.assertLogs("btrfs_restore", level="WARNING"):
            snaps = self.scanner().scan_target_snapshots()
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0].status, "unknown")


if __name__ == "__main__":
    unittest.main()
