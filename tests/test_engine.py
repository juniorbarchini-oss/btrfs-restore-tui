"""
Unit tests for RestoreEngine and conflict resolution.
"""
import tempfile
import unittest
from pathlib import Path

from btrfs_restore.engine import ConflictResolution, RestoreEngine


class TestRestoreEngine(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.snap_root = self.base_path / "snapshot_source"
        self.dest_root = self.base_path / "restore_target"

        self.snap_root.mkdir(parents=True)
        self.dest_root.mkdir(parents=True)

        # Create test source files
        self.test_file1 = self.snap_root / "test1.txt"
        self.test_file1.write_text("snapshot file 1 content")

        self.sub_dir = self.snap_root / "subfolder"
        self.sub_dir.mkdir()
        self.test_file2 = self.sub_dir / "test2.txt"
        self.test_file2.write_text("snapshot file 2 nested content")

        self.engine = RestoreEngine()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_prepare_items(self):
        items = self.engine.prepare_items([self.test_file1, self.sub_dir], self.snap_root)
        self.assertEqual(len(items), 2)
        f_item = next(i for i in items if not i.is_dir)
        d_item = next(i for i in items if i.is_dir)

        self.assertEqual(f_item.rel_path, Path("test1.txt"))
        self.assertEqual(d_item.rel_path, Path("subfolder"))
        self.assertGreater(d_item.size_bytes, 0)

    def test_restore_clean(self):
        items = self.engine.prepare_items([self.test_file1, self.sub_dir], self.snap_root)
        gen = self.engine.restore_generator(items, self.dest_root, ConflictResolution.OVERWRITE)
        states = list(gen)

        self.assertTrue(states[-1].done)
        self.assertTrue((self.dest_root / "test1.txt").exists())
        self.assertTrue((self.dest_root / "subfolder" / "test2.txt").exists())
        self.assertEqual((self.dest_root / "test1.txt").read_text(), "snapshot file 1 content")

    def test_restore_with_backup_conflict(self):
        target_file = self.dest_root / "test1.txt"
        target_file.write_text("current modified file")

        items = self.engine.prepare_items([self.test_file1], self.snap_root)
        gen = self.engine.restore_generator(items, self.dest_root, ConflictResolution.BACKUP)
        states = list(gen)

        self.assertTrue(states[-1].done)
        self.assertEqual(target_file.read_text(), "snapshot file 1 content")
        bak_file = self.dest_root / "test1.txt.bak"
        self.assertTrue(bak_file.exists())
        self.assertEqual(bak_file.read_text(), "current modified file")


if __name__ == "__main__":
    unittest.main()
