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
from btrfs_restore.config import Config, BACKUP_DIRNAME


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

    def test_percent_clamped(self):
        self.cfg_file.write_text("MAX_DISK_PERCENT=5\n")
        self.assertEqual(Config.load().max_disk_percent, 10)  # floor
        self.cfg_file.write_text("MAX_DISK_PERCENT=150\n")
        self.assertEqual(Config.load().max_disk_percent, 99)  # ceil


if __name__ == "__main__":
    unittest.main()
