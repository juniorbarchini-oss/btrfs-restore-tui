"""Pre-run "this would be a FULL backup" confirmation in backup-now."""
import unittest
from unittest import mock

from btrfs_restore import cli_backup
from btrfs_restore.backup import RemoteQueryError


class _Cfg:
    remote_host = "10.0.0.9"
    remote_path = "/srv/backups"


def _engine(full=None, error=None):
    eng = mock.Mock()
    if error:
        eng.remote_full_kinds.side_effect = error
    else:
        eng.remote_full_kinds.return_value = full
    return eng


class TestConfirmRemoteFull(unittest.TestCase):
    def _run(self, eng, answer):
        with mock.patch.object(cli_backup, "BtrfsBackupEngine", return_value=eng), \
             mock.patch.object(cli_backup.console, "print"), \
             mock.patch("builtins.input", side_effect=answer) as inp:
            return cli_backup._confirm_remote_full(_Cfg()), inp

    def test_incremental_possible_asks_nothing(self):
        ok, inp = self._run(_engine(full=[]), ["n"])
        self.assertTrue(ok)
        inp.assert_not_called()

    def test_full_asks_and_yes_continues(self):
        ok, inp = self._run(_engine(full=["home"]), ["y"])
        self.assertTrue(ok)
        inp.assert_called_once()

    def test_full_asks_and_default_is_no(self):
        ok, _ = self._run(_engine(full=["home"]), [""])
        self.assertFalse(ok)

    def test_unreachable_remote_asks_and_no_tty_means_no(self):
        ok, _ = self._run(_engine(error=RemoteQueryError("timed out")), EOFError())
        self.assertFalse(ok)

    def test_unreachable_remote_prompt_says_could_not_check(self):
        eng = _engine(error=RemoteQueryError("timed out after 25s"))
        with mock.patch.object(cli_backup, "BtrfsBackupEngine", return_value=eng), \
             mock.patch.object(cli_backup.console, "print") as pr, \
             mock.patch("builtins.input", return_value="n"):
            cli_backup._confirm_remote_full(_Cfg())
        shown = str(pr.call_args_list[0].args[0].renderable)
        self.assertIn("could not check", shown)
        self.assertIn("timed out after 25s", shown)


if __name__ == "__main__":
    unittest.main()
