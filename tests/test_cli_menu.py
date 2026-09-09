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
        with mock.patch.object(cli, "_run_backup") as rb, \
             mock.patch.object(sys, "argv", ["restore-tui", "backup", "--dry-run"]):
            cli.main()
        rb.assert_called_once_with(["--dry-run"])

    def test_restore_subcommand_routes_to_restore(self):
        with mock.patch.object(cli, "_run_restore") as rr, \
             mock.patch.object(sys, "argv", ["restore-tui", "restore"]):
            cli.main()
        rr.assert_called_once()

    def test_menu_q_exits_cleanly(self):
        with mock.patch.object(sys, "argv", ["restore-tui"]), \
             mock.patch.object(cli.console, "input", return_value="q"), \
             mock.patch.object(cli.console, "print"):
            with self.assertRaises(SystemExit) as e:
                cli.main()
        self.assertEqual(e.exception.code, 0)

    def test_menu_b_then_backup(self):
        calls = iter(["b"])
        with mock.patch.object(sys, "argv", ["restore-tui"]), \
             mock.patch.object(cli.console, "input", side_effect=lambda *_: next(calls)), \
             mock.patch.object(cli.console, "print"), \
             mock.patch.object(cli, "_run_backup") as rb:
            rb.side_effect = SystemExit(0)   # _run_backup normally execs away
            with self.assertRaises(SystemExit):
                cli.main()
        rb.assert_called_once_with([])

    def test_menu_rejects_garbage_then_quits(self):
        answers = iter(["xyz", "q"])
        seen = []
        with mock.patch.object(sys, "argv", ["restore-tui"]), \
             mock.patch.object(cli.console, "input", side_effect=lambda *_: next(answers)), \
             mock.patch.object(cli.console, "print", side_effect=lambda *a, **k: seen.append(a)):
            with self.assertRaises(SystemExit):
                cli.main()
        self.assertTrue(any("Opcion" in str(a) for a in seen))

    def test_elevate_builds_module_command_when_root(self):
        with mock.patch.object(cli.os, "geteuid", return_value=0), \
             mock.patch.object(cli.os, "execvp") as ex, \
             mock.patch.dict(cli.os.environ, {}, clear=True):
            cli._elevate_into("btrfs_restore.cli_backup", ["--dry-run"])
        argv = ex.call_args[0][1]
        self.assertIn("-m", argv)
        self.assertIn("btrfs_restore.cli_backup", argv)
        self.assertIn("--dry-run", argv)


if __name__ == "__main__":
    unittest.main()
