"""
restore-tui entry point: menu choices and subcommand dispatch.
"""
import io
import sys
import unittest
from unittest import mock

from btrfs_restore import cli


class TestCliDispatch(unittest.TestCase):
    def test_backup_subcommand_routes_to_backup(self):
        with mock.patch.object(cli, "_run_backup", return_value=0) as rb, \
             mock.patch.object(sys, "argv", ["restore-tui", "backup", "--dry-run"]):
            with self.assertRaises(SystemExit):
                cli.main()
        rb.assert_called_once_with(["--dry-run"])

    def test_restore_subcommand_routes_to_restore(self):
        with mock.patch.object(cli, "_run_restore", return_value=0) as rr, \
             mock.patch.object(sys, "argv", ["restore-tui", "restore"]):
            with self.assertRaises(SystemExit):
                cli.main()
        rr.assert_called_once()

    def test_gc_and_paths_route(self):
        with mock.patch.object(cli, "_gc", return_value=0) as g, \
             mock.patch.object(sys, "argv", ["restore-tui", "--gc"]):
            with self.assertRaises(SystemExit):
                cli.main()
        g.assert_called_once()
        with mock.patch.object(cli, "_paths", return_value=0) as p, \
             mock.patch.object(sys, "argv", ["restore-tui", "--paths"]):
            with self.assertRaises(SystemExit):
                cli.main()
        p.assert_called_once()

    def test_paths_lists_known_locations(self):
        with mock.patch.object(cli.console, "print") as pr:
            cli._paths()
        out = " ".join(str(c.args[0]) for c in pr.call_args_list if c.args)
        self.assertIn("/opt/btrfs-restore-tui/", out)
        self.assertIn(".config/btrfs-restore/config.conf", out)
        self.assertIn("/.snapshots/staging/", out)

    def test_menu_q_exits_cleanly(self):
        with mock.patch.object(sys, "argv", ["restore-tui"]), \
             mock.patch.object(cli.console, "input", return_value="q"), \
             mock.patch.object(cli.console, "print"):
            with self.assertRaises(SystemExit) as e:
                cli.main()
        self.assertEqual(e.exception.code, 0)

    def test_menu_b_runs_backup_then_returns_to_menu(self):
        answers = iter(["b", "q"])
        with mock.patch.object(sys, "argv", ["restore-tui"]), \
             mock.patch.object(cli.console, "input", side_effect=lambda *_: next(answers)), \
             mock.patch.object(cli.console, "print"), \
             mock.patch.object(cli, "_print_menu") as pm, \
             mock.patch.object(cli, "_run_backup", return_value=0) as rb:
            with self.assertRaises(SystemExit) as e:
                cli.main()
        rb.assert_called_once()
        self.assertEqual(e.exception.code, 0)
        # menu redrawn after backup returned (initial + after-backup)
        self.assertGreaterEqual(pm.call_count, 2)

    def test_menu_rejects_garbage_then_quits(self):
        answers = iter(["xyz", "q"])
        seen = []
        with mock.patch.object(sys, "argv", ["restore-tui"]), \
             mock.patch.object(cli.console, "input", side_effect=lambda *_: next(answers)), \
             mock.patch.object(cli.console, "print", side_effect=lambda *a, **k: seen.append(a)):
            with self.assertRaises(SystemExit):
                cli.main()
        self.assertTrue(any("Pick one" in str(a) for a in seen))

    def test_run_mode_builds_module_command_and_returns_code(self):
        with mock.patch.object(cli.os, "geteuid", return_value=0), \
             mock.patch.object(cli.subprocess, "run",
                               return_value=mock.Mock(returncode=7)) as sr, \
             mock.patch.dict(cli.os.environ, {}, clear=True):
            rc = cli._run_mode("btrfs_restore.cli_backup", ["--dry-run"])
        self.assertEqual(rc, 7)
        argv = sr.call_args[0][0]
        self.assertIn("-m", argv)
        self.assertIn("btrfs_restore.cli_backup", argv)
        self.assertIn("--dry-run", argv)
        self.assertNotIn("sudo", argv)   # already root

    def test_run_mode_prepends_sudo_when_not_root(self):
        with mock.patch.object(cli.os, "geteuid", return_value=1000), \
             mock.patch.object(cli.subprocess, "run",
                               return_value=mock.Mock(returncode=0)) as sr:
            cli._run_mode("btrfs_restore.cli_backup")
        self.assertEqual(sr.call_args[0][0][0], "sudo")


if __name__ == "__main__":
    unittest.main()
