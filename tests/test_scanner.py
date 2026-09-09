"""
Unit tests for SnapshotScanner with native Btrfs subvolumes and Snapper.
"""
import tempfile
import unittest
from pathlib import Path

from btrfs_restore.scanner import SnapshotScanner


class TestSnapshotScanner(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.snap_dir = Path(self.temp_dir.name)

        # Simulated structure
        (self.snap_dir / "home_parent" / "testuser").mkdir(parents=True)
        (self.snap_dir / "root_parent").mkdir()
        (self.snap_dir / "home_20260906_153000" / "testuser").mkdir(parents=True)
        (self.snap_dir / "root_20260906_153000").mkdir()

        # Simulated Snapper
        snapper_1 = self.snap_dir / "1"
        snapper_1.mkdir()
        (snapper_1 / "snapshot").mkdir()
        (snapper_1 / "info.xml").write_text(
            "<snapshot><date>2026-09-06 10:00:00</date><description>Pre-upgrade</description><num>1</num></snapshot>"
        )

        self.scanner = SnapshotScanner(snapshots_dir=self.snap_dir)
        self.scanner.user = "testuser"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_scan_local_snapshots(self):
        snaps = self.scanner.scan_local_snapshots()
        self.assertEqual(len(snaps), 5)
        ids = [s.id for s in snaps]
        self.assertIn("local_home_parent", ids)
        self.assertIn("local_root_parent", ids)
        self.assertIn("local_home_20260906_153000", ids)
        self.assertIn("local_root_20260906_153000", ids)
        self.assertIn("snapper_1", ids)

        home_snap = next(s for s in snaps if s.id == "local_home_parent")
        self.assertEqual(home_snap.path, self.snap_dir / "home_parent" / "testuser")

        dated_snap = next(s for s in snaps if s.id == "local_home_20260906_153000")
        self.assertEqual(dated_snap.path, self.snap_dir / "home_20260906_153000" / "testuser")


if __name__ == "__main__":
    unittest.main()
