"""
cli_pbs: latest-snapshot resolution and the "not configured" / "no snapshots"
guard rails. The actual `proxmox-backup-client` invocation is not exercised
here (that's an integration concern, not a unit one).
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from btrfs_restore.cli_pbs import _latest_snapshot


class TestLatestSnapshot(unittest.TestCase):
    def test_picks_newest_by_name(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            for name in ("root_20260910_150549", "root_20260913_234101", "root_20260909_230829"):
                (base / name).mkdir()
            self.assertEqual(
                _latest_snapshot(base, "root").name, "root_20260913_234101"
            )

    def test_ignores_other_kind_and_files(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            (base / "home_20260913_234101").mkdir()
            (base / "root_20260912_000000").mkdir()
            (base / "root_20260913_999999").touch()  # a file, not a dir - must be skipped
            self.assertEqual(
                _latest_snapshot(base, "root").name, "root_20260912_000000"
            )

    def test_ignores_symlinks(self):
        with TemporaryDirectory() as d:
            base = Path(d)
            real = base / "root_20260910_000000"
            real.mkdir()
            (base / "root_20260999_999999").symlink_to(real)  # e.g. root_parent-style link
            self.assertEqual(
                _latest_snapshot(base, "root").name, "root_20260910_000000"
            )

    def test_no_snapshots_returns_none(self):
        with TemporaryDirectory() as d:
            self.assertIsNone(_latest_snapshot(Path(d), "root"))

    def test_missing_dir_returns_none(self):
        self.assertIsNone(_latest_snapshot(Path("/nonexistent-xyz"), "root"))


if __name__ == "__main__":
    unittest.main()
