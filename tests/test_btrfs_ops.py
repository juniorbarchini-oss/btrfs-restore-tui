"""
btrfs_ops: argv-only command construction (no shell), progress plumbing.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

from btrfs_restore.btrfs_ops import BtrfsOps, _human, prune_paths


class TestArgvSafety(unittest.TestCase):
    def test_send_argv_is_a_list_never_a_string(self):
        ops = BtrfsOps()
        argv = ops._send_argv(Path("/mnt/x'; touch /tmp/pwned; '"), None)
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[:2], ["btrfs", "send"])
        # the nasty path is a single opaque argument, not shell tokens
        self.assertEqual(argv[-1], "/mnt/x'; touch /tmp/pwned; '")

    def test_send_argv_with_parent(self):
        ops = BtrfsOps()
        argv = ops._send_argv(Path("/s/new"), Path("/s/old"))
        self.assertEqual(argv, ["btrfs", "send", "-p", "/s/old", "/s/new"])


class TestCountedPipe(unittest.TestCase):
    def test_run_counted_moves_bytes_and_reports(self):
        seen = []
        ops = BtrfsOps(progress_cb=seen.append)
        # force the built-in counter path regardless of whether pv is installed
        ops._use_pv, ops._count = False, True
        with open("/dev/null", "wb") as out:
            rc = ops._run_counted(["head", "-c", "3000000", "/dev/zero"], ["cat"], out)
        self.assertEqual(rc, 0)
        self.assertTrue(seen and "sent" in seen[-1])

    def test_pipe_failure_propagates(self):
        ops = BtrfsOps(progress_cb=lambda s: None)
        ops._use_pv, ops._count = False, True
        with open("/dev/null", "wb") as out:
            rc = ops._run_counted(["false"], ["cat"], out)
        self.assertNotEqual(rc, 0)


class TestPrunePaths(unittest.TestCase):
    def test_removes_matches_keeps_the_rest_and_skips_subvolumes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "alice" / ".cache" / "x").mkdir(parents=True)
            (root / "alice" / ".cache" / "x" / "blob").write_text("junk")
            (root / "alice" / "Documents").mkdir(parents=True)
            (root / "alice" / "Documents" / "keep.txt").write_text("keep")
            (root / "var" / "log").mkdir(parents=True)          # nested subvol
            (root / "var" / "tmp" / "j").mkdir(parents=True)

            removed = prune_paths(
                root, ["*/.cache", "var/tmp/*", "var/log"],
                is_subvolume=lambda p: p.name == "log",
            )
            self.assertIn("alice/.cache", removed)
            self.assertIn("var/tmp/j", removed)
            self.assertNotIn("var/log", removed)                # subvolume left alone
            self.assertFalse((root / "alice" / ".cache").exists())
            self.assertTrue((root / "var" / "log").exists())
            self.assertTrue((root / "alice" / "Documents" / "keep.txt").exists())

    def test_missing_glob_is_a_noop(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(prune_paths(Path(d), ["nope/*", "*/.cache"]), [])


class TestHuman(unittest.TestCase):
    def test_units(self):
        self.assertEqual(_human(0), "0.0 B")
        self.assertEqual(_human(1536), "1.5 KiB")
        self.assertEqual(_human(5 * 1024**3), "5.0 GiB")


if __name__ == "__main__":
    unittest.main()
