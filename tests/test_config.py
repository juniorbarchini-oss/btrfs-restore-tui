"""
Config loader: schema convergence with the ext4 edition, legacy aliases,
resolution order, and the no-rewrite rule.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from btrfs_restore import config as cfgmod
from btrfs_restore.config import Config, BACKUP_DIRNAME, list_backup_drives, save_user_settings


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / ".config" / "restore-tui").mkdir(parents=True)
        self.cfg_file = self.home / ".config" / "restore-tui" / "config.conf"
        # Isolate: fake home, no env, no autodetected USB.
        self._patchers = [
            mock.patch.object(cfgmod, "_home_of", return_value=self.home),
            mock.patch.object(cfgmod, "_autodetect_backup_target", return_value=None),
            mock.patch.dict(os.environ, {}, clear=True),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self.tmp.cleanup()

    def test_defaults(self):
        cfg = Config.load()
        self.assertEqual(cfg.max_disk_percent, 80)
        self.assertEqual(cfg.min_keep, 2)
        self.assertEqual(cfg.max_snapshots, 0)
        self.assertIsNone(cfg.target_root)
        self.assertEqual(cfg.source_mounts, ["/", "/home"])

    def test_file_is_read_and_never_rewritten(self):
        original = (
            "TARGET_DIR=/mnt/usb\n"
            "MAX_DISK_PERCENT=70\n"
            "MIN_KEEP=3\n"
            "REMOTE_HOST=10.0.0.5\n"
            "REMOTE_PATH=/backups\n"
        )
        self.cfg_file.write_text(original)
        cfg = Config.load()
        self.assertEqual(cfg.target_root, Path("/mnt/usb") / BACKUP_DIRNAME)
        self.assertEqual(cfg.max_disk_percent, 70)
        self.assertEqual(cfg.min_keep, 3)
        self.assertEqual(cfg.remote_host, "10.0.0.5")
        self.assertEqual(cfg.remote_name, "10.0.0.5")  # falls back to host
        self.assertEqual(self.cfg_file.read_text(), original)

    def test_legacy_btrfs_remote_keys_still_work(self):
        self.cfg_file.write_text(
            "BTRFS_REMOTE_HOST=1.2.3.4\n"
            "BTRFS_REMOTE_PATH=/srv/b\n"
            "BTRFS_REMOTE_PORT=2222\n"
            "BTRFS_REMOTE_NAME=nas\n"
        )
        cfg = Config.load()
        self.assertEqual(cfg.remote_host, "1.2.3.4")
        self.assertEqual(cfg.remote_path, "/srv/b")
        self.assertEqual(cfg.remote_port, 2222)
        self.assertEqual(cfg.remote_name, "nas")

    def test_env_overrides_file(self):
        self.cfg_file.write_text("MAX_DISK_PERCENT=50\nTARGET_DIR=/mnt/fromfile\n")
        with mock.patch.dict(os.environ, {
            "RESTORE_TUI_MAX_DISK_PERCENT": "91",
            "RESTORE_TUI_TARGET_DIR": "/mnt/fromenv",
        }):
            cfg = Config.load()
        self.assertEqual(cfg.max_disk_percent, 91)
        self.assertEqual(cfg.target_root, Path("/mnt/fromenv") / BACKUP_DIRNAME)

    def test_legacy_env_prefix(self):
        with mock.patch.dict(os.environ, {"BTRFS_REMOTE_HOST": "9.9.9.9"}):
            cfg = Config.load()
        self.assertEqual(cfg.remote_host, "9.9.9.9")

    def test_target_dir_accepts_mount_or_suffixed(self):
        for given in ("/mnt/x", f"/mnt/x/{BACKUP_DIRNAME}"):
            self.cfg_file.write_text(f"TARGET_DIR={given}\n")
            cfg = Config.load()
            self.assertEqual(cfg.target_root, Path("/mnt/x") / BACKUP_DIRNAME)

    def test_derived_paths_match_ext4_shape(self):
        self.cfg_file.write_text("TARGET_DIR=/mnt/x\n")
        cfg = Config.load()
        self.assertEqual(cfg.snapshots_dir, Path("/mnt/x") / BACKUP_DIRNAME / "snapshots")
        self.assertEqual(cfg.latest_link, Path("/mnt/x") / BACKUP_DIRNAME / "latest")

    def test_exclusions_defaults_and_extras(self):
        self.cfg_file.write_text(
            "EXCLUDE=Downloads/big/\n"
            "EXCLUDE=*/node_modules\n"
        )
        cfg = Config.load()
        self.assertTrue(cfg.exclude_defaults)
        home_ex = cfg.exclusions_for("/home")
        self.assertIn("*/.cache", home_ex)          # built-in
        self.assertIn("Downloads/big/", home_ex)    # from file
        self.assertIn("*/node_modules", home_ex)
        root_ex = cfg.exclusions_for("/")
        self.assertIn("var/tmp/*", root_ex)
        self.assertIn("Downloads/big/", root_ex)    # extras apply to every mount

    def test_default_home_list_covers_browser_caches(self):
        home_ex = Config().exclusions_for("/home")
        for pat in ("*/.cache", "*/.config/*/*/Cache", "*/.config/*/*/Code Cache",
                    "*/.config/*/*/GPUCache", "*/.config/*/CachedData",
                    "*/.config/*/*/Service Worker/CacheStorage"):
            self.assertIn(pat, home_ex)

    def test_exclude_defaults_off(self):
        self.cfg_file.write_text("EXCLUDE_DEFAULTS=off\nEXCLUDE=*/.cache\n")
        cfg = Config.load()
        self.assertFalse(cfg.exclude_defaults)
        self.assertEqual(cfg.exclusions_for("/home"), ["*/.cache"])
        self.assertEqual(cfg.exclusions_for("/"), ["*/.cache"])

    def test_percent_clamped(self):
        self.cfg_file.write_text("MAX_DISK_PERCENT=5\n")
        self.assertEqual(Config.load().max_disk_percent, 10)  # floor
        self.cfg_file.write_text("MAX_DISK_PERCENT=150\n")
        self.assertEqual(Config.load().max_disk_percent, 99)  # ceil


class TestSettingsScreen(unittest.TestCase):
    """The Settings screen's write path (config.py) and drive listing -
    the Textual widgets themselves are covered in test_settings_ui.py."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / ".config" / "restore-tui").mkdir(parents=True)
        self.cfg_file = self.home / ".config" / "restore-tui" / "config.conf"
        self._patchers = [
            mock.patch.object(cfgmod, "_home_of", return_value=self.home),
            mock.patch.object(cfgmod, "_current_user", return_value="tester"),
            mock.patch.dict(os.environ, {}, clear=True),
        ]
        for p in self._patchers:
            p.start()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self.tmp.cleanup()

    def test_save_creates_file_with_updates(self):
        path = save_user_settings({"TARGET_DIR": "/mnt/usb", "REMOTE_HOST": ""})
        self.assertEqual(path, self.cfg_file)
        text = self.cfg_file.read_text()
        self.assertIn("TARGET_DIR=/mnt/usb", text)
        self.assertIn("REMOTE_HOST=", text)

    def test_save_preserves_untouched_lines_and_comments(self):
        self.cfg_file.write_text(
            "# my notes\n"
            "MAX_DISK_PERCENT=70\n"
            "EXCLUDE=Downloads/big/\n"
            "TARGET_DIR=/mnt/old\n"
        )
        save_user_settings({"TARGET_DIR": "/mnt/new"})
        lines = self.cfg_file.read_text().splitlines()
        self.assertIn("# my notes", lines)
        self.assertIn("MAX_DISK_PERCENT=70", lines)
        self.assertIn("EXCLUDE=Downloads/big/", lines)
        self.assertIn("TARGET_DIR=/mnt/new", lines)
        self.assertNotIn("TARGET_DIR=/mnt/old", lines)
        # rewritten key stays on one line, no duplicate
        self.assertEqual(sum(1 for l in lines if l.startswith("TARGET_DIR=")), 1)

    def test_save_round_trips_through_config_load(self):
        save_user_settings({
            "TARGET_DIR": "/mnt/pinned",
            "REMOTE_HOST": "10.0.0.9",
            "REMOTE_USER": "hbarchini",
            "REMOTE_PATH": "/backups",
            "REMOTE_PORT": "2222",
            "REMOTE_NAME": "nas",
        })
        with mock.patch.object(cfgmod, "_autodetect_backup_target", return_value=None):
            cfg = Config.load()
        self.assertEqual(cfg.target_root, Path("/mnt/pinned") / BACKUP_DIRNAME)
        self.assertEqual(cfg.remote_host, "10.0.0.9")
        self.assertEqual(cfg.remote_port, 2222)
        self.assertEqual(cfg.remote_name, "nas")

    def test_save_empty_target_dir_falls_back_to_autodetect(self):
        self.cfg_file.write_text("TARGET_DIR=/mnt/old\n")
        save_user_settings({"TARGET_DIR": ""})
        with mock.patch.object(cfgmod, "_autodetect_backup_target", return_value="/mnt/detected"):
            cfg = Config.load()
        self.assertEqual(cfg.target_root, Path("/mnt/detected") / BACKUP_DIRNAME)

    def test_list_backup_drives_reports_fstype(self):
        media = self.home  # any writable dir stands in for a mount base
        fake_base = Path(self.tmp.name) / "media_base"
        (fake_base / "USB1").mkdir(parents=True)
        with mock.patch.object(cfgmod, "_candidate_drives", return_value=[fake_base / "USB1"]), \
             mock.patch.object(cfgmod, "_fstype_of", return_value="ext4"):
            drives = list_backup_drives("tester")
        self.assertEqual(drives, [(fake_base / "USB1", "ext4")])

    def test_list_backup_drives_empty_when_none_connected(self):
        with mock.patch.object(cfgmod, "_candidate_drives", return_value=[]):
            self.assertEqual(list_backup_drives("tester"), [])


if __name__ == "__main__":
    unittest.main()
