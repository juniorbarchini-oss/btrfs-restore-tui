"""
deploy_staging builds argv-list pipelines - no shell, no interpolation (#9).
"""
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from btrfs_restore import config as cfgmod
from btrfs_restore.config import Config
from btrfs_restore.engine import RestoreEngine
from btrfs_restore.models import SnapshotInfo, SnapshotType


def _snap(**kw):
    d = dict(id="x", name="x", path=Path("/some/where"), snap_type=SnapshotType.REMOTE,
             timestamp=datetime(2026, 9, 9), is_subvolume=True)
    d.update(kw)
    return SnapshotInfo(**d)


class TestStagingStages(unittest.TestCase):
    def setUp(self):
        self._p = [
            mock.patch.object(cfgmod, "_home_of", return_value=Path("/nonexistent")),
            mock.patch.object(cfgmod, "_autodetect_backup_target", return_value=None),
            mock.patch.dict("os.environ", {"USER": "bob"}, clear=True),
        ]
        for p in self._p:
            p.start()
        self.eng = RestoreEngine()
        self.eng.staging_dir = Path("/tmp/stg")

    def tearDown(self):
        for p in self._p:
            p.stop()

    def _cfg(self, **kw):
        c = Config()
        c.remote_host = kw.get("host", "10.0.0.9")
        c.remote_user = kw.get("user", "bob")
        c.remote_port = kw.get("port", 22)
        return c

    def test_local_needs_no_staging(self):
        s = _snap(snap_type=SnapshotType.LOCAL)
        self.assertIsNone(self.eng._staging_stages(s, self._cfg(), "bob"))

    def test_usb_subvolume_returned_directly(self):
        s = _snap(snap_type=SnapshotType.USB, is_subvolume=True, path=Path("/run/media/bob/USB/root_x"))
        self.assertEqual(self.eng.deploy_staging(s), Path("/run/media/bob/USB/root_x"))

    def test_remote_subvolume_pipeline_is_argv_lists(self):
        s = _snap(is_subvolume=True, path=Path("/backups/home_x"))
        stages = self.eng._staging_stages(s, self._cfg(), "bob")
        self.assertEqual(len(stages), 2)
        self.assertTrue(all(isinstance(st, list) for st in stages))
        self.assertEqual(stages[0][:2], ["ssh", "-o"])
        self.assertEqual(stages[0][-4:], ["sudo", "btrfs", "send", "/backups/home_x"])
        self.assertEqual(stages[1], ["btrfs", "receive", "/tmp/stg"])

    def test_remote_stream_has_zstd_stage(self):
        s = _snap(is_subvolume=False, path=Path("/backups/home_x.btrfs.zst"))
        stages = self.eng._staging_stages(s, self._cfg(), "bob")
        self.assertEqual([st[0] for st in stages], ["ssh", "zstd", "btrfs"])

    def test_malicious_path_stays_one_argument(self):
        evil = "/backups/x\"; rm -rf / #"
        s = _snap(is_subvolume=True, path=Path(evil))
        stages = self.eng._staging_stages(s, self._cfg(), "bob")
        self.assertIn(evil, stages[0])          # present verbatim as ONE element
        self.assertEqual(stages[0].count(evil), 1)

    def test_port_and_key_options(self):
        with mock.patch("pathlib.Path.exists", return_value=True):
            stages = self.eng._staging_stages(_snap(path=Path("/b/h")),
                                              self._cfg(port=2222), "bob")
        argv = stages[0]
        self.assertIn("-p", argv)
        self.assertIn("2222", argv)
        self.assertIn("-i", argv)

    def test_remote_without_host_raises(self):
        with self.assertRaises(RuntimeError):
            self.eng._staging_stages(_snap(), self._cfg(host=""), "bob")


if __name__ == "__main__":
    unittest.main()
