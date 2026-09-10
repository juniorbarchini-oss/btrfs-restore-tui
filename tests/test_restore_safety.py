"""
#10 (a per-file error must not report success, must not abort the whole run,
must not be overwritten by later progress) and #11 (ownership on a restore to
a system path preserves the snapshot's owner, not the invoking user).
"""
import errno
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from btrfs_restore.engine import RestoreEngine
from btrfs_restore.models import ConflictResolution


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.snap = root / "snap"
        self.dest = root / "dest"
        self.snap.mkdir()
        self.dest.mkdir()
        for n in ("a.txt", "b.txt", "c.txt"):
            (self.snap / n).write_text(n)
        self.engine = RestoreEngine()

    def tearDown(self):
        self.tmp.cleanup()

    def items(self, *names):
        paths = [self.snap / n for n in names]
        return self.engine.prepare_items(paths, self.snap)


class TestPerFileErrors(Base):
    def test_one_failing_file_does_not_abort_and_final_state_is_not_success(self):
        real_copy = shutil.copy2

        def flaky(src, dst, *a, **k):
            if Path(src).name == "b.txt":
                raise PermissionError(13, "Permission denied")
            return real_copy(src, dst, *a, **k)

        with mock.patch("btrfs_restore.engine.shutil.copy2", side_effect=flaky):
            states = list(self.engine.restore_generator(
                self.items("a.txt", "b.txt", "c.txt"), self.dest,
                ConflictResolution.OVERWRITE))

        final = states[-1]
        self.assertTrue(final.done)
        self.assertIsNotNone(final.error)                 # NOT a bare success
        self.assertEqual(final.failed_files, 1)
        self.assertIn("b.txt", final.error)
        self.assertFalse(final.fatal)
        # the other two were still restored
        self.assertTrue((self.dest / "a.txt").exists())
        self.assertTrue((self.dest / "c.txt").exists())
        self.assertFalse((self.dest / "b.txt").exists())
        # the error is present on the terminal state, not only a transient one
        self.assertEqual(final.processed_files, 2)

    def test_fatal_when_target_not_writable(self):
        bad = Path("/proc/nonexistent-dir/xyz")   # mkdir will fail
        states = list(self.engine.restore_generator(
            self.items("a.txt"), bad, ConflictResolution.OVERWRITE))
        self.assertEqual(len(states), 1)
        self.assertTrue(states[0].done)
        self.assertTrue(states[0].fatal)
        self.assertIn("Cannot write", states[0].error)

    def test_disk_full_aborts(self):
        def enospc(src, dst, *a, **k):
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch("btrfs_restore.engine.shutil.copy2", side_effect=enospc):
            states = list(self.engine.restore_generator(
                self.items("a.txt", "b.txt", "c.txt"), self.dest,
                ConflictResolution.OVERWRITE))
        final = states[-1]
        self.assertTrue(final.done and final.fatal)
        self.assertIn("Disk full", final.error)
        # aborted early - did not try every file
        self.assertEqual(final.processed_files, 0)


class TestEmptyDirs(Base):
    def test_empty_subdirs_are_recreated(self):
        tree = self.snap / "project"
        (tree / "src").mkdir(parents=True)
        (tree / "src" / "main.py").write_text("x")
        (tree / "empty").mkdir()                       # empty
        (tree / "nested" / "deeper").mkdir(parents=True)  # only empty subdirs
        (tree / "logs").mkdir()

        items = self.engine.prepare_items([tree], self.snap)
        states = list(self.engine.restore_generator(
            items, self.dest, ConflictResolution.OVERWRITE))
        self.assertTrue(states[-1].done and not states[-1].error)

        out = self.dest / "project"
        self.assertTrue((out / "src" / "main.py").exists())
        self.assertTrue((out / "empty").is_dir())
        self.assertTrue((out / "nested" / "deeper").is_dir())
        self.assertTrue((out / "logs").is_dir())

    def test_selecting_a_single_empty_dir_restores_it(self):
        (self.snap / "onlydir").mkdir()
        items = self.engine.prepare_items([self.snap / "onlydir"], self.snap)
        list(self.engine.restore_generator(items, self.dest, ConflictResolution.OVERWRITE))
        self.assertTrue((self.dest / "onlydir").is_dir())


class TestOwnership(Base):
    def test_within_user_home_detection(self):
        self.engine._user = "alice"
        self.assertTrue(self.engine._within_user_home(Path("/home/alice")))
        self.assertTrue(self.engine._within_user_home(Path("/home/alice/Documents/x")))
        self.assertFalse(self.engine._within_user_home(Path("/")))
        self.assertFalse(self.engine._within_user_home(Path("/etc")))
        self.assertFalse(self.engine._within_user_home(Path("/home/bob")))

    def test_system_restore_preserves_source_owner(self):
        seen = []

        def spy_chown(path, uid, gid, *a, **k):
            seen.append((str(path), uid, gid))

        # snapshot file "owned by root" (uid 0) per its stat
        fake_stat = os.stat_result((0o100644, 0, 0, 1, 0, 0, 5, 0, 0, 0))
        with mock.patch("btrfs_restore.engine.os.chown", side_effect=spy_chown), \
             mock.patch.object(Path, "stat", return_value=fake_stat):
            list(self.engine.restore_generator(
                self.items("a.txt"), self.dest,
                ConflictResolution.OVERWRITE, preserve_system_ownership=True))

        # the restored file was chowned to 0:0 (source), never to the invoker
        dest_chowns = [c for c in seen if c[0].endswith("dest/a.txt")]
        self.assertTrue(dest_chowns)
        self.assertTrue(all(c[1] == 0 and c[2] == 0 for c in dest_chowns))

    def test_home_restore_chowns_to_user(self):
        self.engine.target_uid, self.engine.target_gid = 4242, 4242
        seen = []
        with mock.patch("btrfs_restore.engine.os.chown",
                        side_effect=lambda p, u, g, *a, **k: seen.append((str(p), u, g))):
            list(self.engine.restore_generator(
                self.items("a.txt"), self.dest,
                ConflictResolution.OVERWRITE, preserve_system_ownership=False))
        dest_chowns = [c for c in seen if c[0].endswith("dest/a.txt")]
        self.assertTrue(dest_chowns)
        self.assertTrue(all(c[1] == 4242 and c[2] == 4242 for c in dest_chowns))


if __name__ == "__main__":
    unittest.main()
