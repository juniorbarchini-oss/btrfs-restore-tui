"""Issue #26: real sender/receiver stderr reaches the error, and alerts are
delivered to the real user, deduplicated and cleared on success."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from btrfs_restore import notify
from btrfs_restore.btrfs_ops import BtrfsOps

SEND = ["sh", "-c", "echo payload; echo send-side-warning >&2"]
SINK_FAIL = ["sh", "-c", "cat >/dev/null; echo 'ERROR: cannot receive: bad parent' >&2; exit 3"]
SINK_OK = ["sh", "-c", "cat >/dev/null"]


class TestStderrCapture(unittest.TestCase):
    def test_plain_pipeline_keeps_receiver_error(self):
        ops = BtrfsOps()
        self.assertEqual(ops._send_pipe(SEND, SINK_FAIL), 3)
        self.assertIn("cannot receive: bad parent", ops.last_stderr)
        self.assertIn("send-side-warning", ops.last_stderr)

    def test_counted_pipeline_keeps_receiver_error(self):
        with mock.patch("btrfs_restore.btrfs_ops.shutil.which", return_value=None):
            ops = BtrfsOps(progress_cb=lambda _t: None)
        self.assertEqual(ops._send_pipe(SEND, SINK_FAIL), 3)
        self.assertIn("cannot receive: bad parent", ops.last_stderr)

    def test_stderr_resets_on_the_next_successful_run(self):
        ops = BtrfsOps()
        ops._send_pipe(SEND, SINK_FAIL)
        self.assertEqual(ops._send_pipe(["true"], SINK_OK), 0)
        self.assertEqual(ops.last_stderr, "")


class TestAlert(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.state = self.tmp / ".last-alert"
        self.user = "root"          # a user that always exists for pwd lookups

    def _send(self, **kw):
        run = kw.pop("run", mock.Mock(return_value=mock.Mock(returncode=0)))
        ok = notify.send_alert("T", "body", self.user, self.state, run=run, **kw)
        return ok, run

    def test_root_delivers_to_the_users_session_bus(self):
        ok, run = self._send(is_root=True, exists=lambda p: True)
        self.assertTrue(ok)
        cmd = run.call_args.args[0]
        self.assertEqual(cmd[:4], ["runuser", "-u", self.user, "--"])
        self.assertIn("notify-send", cmd)
        self.assertTrue(any(c.startswith("DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/")
                            for c in cmd))

    def test_no_graphical_session_means_no_alert_and_no_crash(self):
        ok, run = self._send(is_root=True, exists=lambda p: False)
        self.assertFalse(ok)
        run.assert_not_called()

    def test_same_alert_is_not_repeated_within_the_window(self):
        self.assertTrue(self._send(is_root=False, now=lambda: 1000.0)[0])
        self.assertFalse(self._send(is_root=False, now=lambda: 1000.0 + 60)[0])
        self.assertTrue(self._send(is_root=False, now=lambda: 1000.0 + notify.REPEAT + 1)[0])

    def test_clear_alert_allows_the_next_one(self):
        self._send(is_root=False, now=lambda: 1000.0)
        notify.clear_alert(self.state)
        self.assertTrue(self._send(is_root=False, now=lambda: 1001.0)[0])

    def test_missing_notify_send_never_raises(self):
        run = mock.Mock(side_effect=FileNotFoundError("notify-send"))
        self.assertFalse(self._send(is_root=False, run=run)[0])

    def test_failed_notify_send_is_not_recorded_as_sent(self):
        run = mock.Mock(return_value=mock.Mock(returncode=1))
        self.assertFalse(self._send(is_root=False, run=run)[0])
        self.assertFalse(self.state.exists())


if __name__ == "__main__":
    unittest.main()
