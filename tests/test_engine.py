"""
Unit tests for RestoreEngine and conflict resolution.
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from btrfs_restore.engine import ConflictResolution, RestoreEngine


_HAVE_RSYNC = shutil.which("rsync") is not None


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


@unittest.skipUnless(_HAVE_RSYNC, "rsync not installed")
class TestRsyncDirRestore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.snap = root / "snap"
        self.dest = root / "dest"
        tree = self.snap / "proj"
        (tree / "src").mkdir(parents=True)
        (tree / "src" / "main.py").write_text("print('hi')\n")
        (tree / "docs").mkdir()
        (tree / "docs" / "readme.md").write_text("# hi\n")
        (tree / "empty").mkdir()
        (tree / "src" / "link.py").symlink_to("main.py")
        self.dest.mkdir()
        self.engine = RestoreEngine()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, resolution=ConflictResolution.OVERWRITE):
        items = self.engine.prepare_items([self.snap / "proj"], self.snap)
        return list(self.engine.restore_generator(items, self.dest, resolution))

    def test_dir_tree_files_symlink_and_empty_dir_all_restored(self):
        states = self._run()
        self.assertTrue(states[-1].done and not states[-1].error)
        out = self.dest / "proj"
        self.assertEqual((out / "src" / "main.py").read_text(), "print('hi')\n")
        self.assertEqual((out / "docs" / "readme.md").read_text(), "# hi\n")
        self.assertTrue((out / "empty").is_dir())
        self.assertTrue((out / "src" / "link.py").is_symlink())
        # progress bar lands at 100%
        self.assertEqual(states[-1].processed_files, states[-1].total_files)

    def test_skip_keeps_an_existing_file(self):
        (self.dest / "proj" / "src").mkdir(parents=True)
        (self.dest / "proj" / "src" / "main.py").write_text("LOCAL EDIT\n")
        self._run(ConflictResolution.SKIP)
        self.assertEqual((self.dest / "proj" / "src" / "main.py").read_text(), "LOCAL EDIT\n")

    def test_backup_moves_the_existing_file_aside(self):
        (self.dest / "proj" / "docs").mkdir(parents=True)
        (self.dest / "proj" / "docs" / "readme.md").write_text("OLD\n")
        self._run(ConflictResolution.BACKUP)
        self.assertEqual((self.dest / "proj" / "docs" / "readme.md").read_text(), "# hi\n")
        self.assertEqual((self.dest / "proj" / "docs" / "readme.md.bak").read_text(), "OLD\n")

    def test_rsync_partial_failure_is_reported_not_fatal(self):
        def fake_rsync(src, dst, res, to_user):
            yield ("progress", 1, 10, "main.py")
            yield ("error", "rsync: mkstemp failed: Permission denied (13)")

        with mock.patch.object(self.engine, "_rsync_item", side_effect=fake_rsync):
            states = self._run()
        final = states[-1]
        self.assertTrue(final.done)
        self.assertFalse(final.fatal)
        self.assertIn("Permission denied", final.error)

    def test_falls_back_to_per_file_copy_without_rsync(self):
        with mock.patch("btrfs_restore.engine.shutil.which", return_value=None):
            states = self._run()
        self.assertTrue(states[-1].done and not states[-1].error)
        self.assertEqual((self.dest / "proj" / "src" / "main.py").read_text(), "print('hi')\n")


if __name__ == "__main__":
    unittest.main()
