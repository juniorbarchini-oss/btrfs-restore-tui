"""
Settings screen widgets: drive picker, SSH toggle, and the save path that
writes to config.conf. Config resolution itself (save_user_settings,
list_backup_drives) is covered in test_config.py.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from textual.widgets import Checkbox, Input

from btrfs_restore import config as cfgmod
from btrfs_restore.settings_ui import SettingsApp


class TestSettingsApp(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / ".config" / "restore-tui").mkdir(parents=True)
        self.cfg_file = self.home / ".config" / "restore-tui" / "config.conf"
        self.usb = Path(self.tmp.name) / "USB1"
        self.usb.mkdir()
        self._patchers = [
            mock.patch.object(cfgmod, "_home_of", return_value=self.home),
            mock.patch.object(cfgmod, "_current_user", return_value="tester"),
            mock.patch.object(cfgmod, "_candidate_drives", return_value=[self.usb]),
            mock.patch.object(cfgmod, "_fstype_of", return_value="ext4"),
            mock.patch.dict(os.environ, {}, clear=True),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self.tmp.cleanup()

    async def test_ssh_checkbox_toggles_input_disabled_state(self):
        app = SettingsApp()
        async with app.run_test(size=(100, 40)) as pilot:
            host_input = app.query_one("#input-host", Input)
            self.assertTrue(host_input.disabled)  # unchecked by default

            checkbox = app.query_one("#remote-enabled", Checkbox)
            checkbox.value = True
            await pilot.pause()
            self.assertFalse(host_input.disabled)

    async def test_save_without_host_while_enabled_shows_error(self):
        app = SettingsApp()
        async with app.run_test(size=(100, 40)) as pilot:
            app.query_one("#remote-enabled", Checkbox).value = True
            await pilot.pause()
            app._save()
            await pilot.pause()
            status = str(app.query_one("#settings-status").render())
            self.assertIn("host", status.lower())
        self.assertFalse(self.cfg_file.exists())

    async def test_save_writes_selected_drive_and_remote(self):
        app = SettingsApp()
        async with app.run_test(size=(100, 40)) as pilot:
            drive_id = app._drive_id(self.usb)
            await pilot.click(f"#{drive_id}")
            app.query_one("#remote-enabled", Checkbox).value = True
            app.query_one("#input-host", Input).value = "10.0.0.9"
            await pilot.pause()

            app._save()
            await pilot.pause()

        text = self.cfg_file.read_text()
        self.assertIn(f"TARGET_DIR={self.usb}", text)
        self.assertIn("REMOTE_HOST=10.0.0.9", text)

    async def test_cancel_does_not_write_config(self):
        app = SettingsApp()
        async with app.run_test(size=(100, 40)) as pilot:
            app.action_cancel()
            await pilot.pause()
        self.assertFalse(self.cfg_file.exists())
        self.assertFalse(app.saved)


if __name__ == "__main__":
    unittest.main()
