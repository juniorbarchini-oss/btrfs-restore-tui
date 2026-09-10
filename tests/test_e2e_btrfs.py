"""
End-to-end exercise of the *real* btrfs backend (BtrfsOps) on a throwaway
loop-device filesystem: snapshot -> send -> receive, then an incremental
`send -p` round-trip.

Opt-in: needs root, `mkfs.btrfs`, and `BTRFS_RESTORE_E2E=1` in the environment
(set by `sudo ./run-tests.sh --e2e`). Skipped everywhere else so the normal
suite stays root-free.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from btrfs_restore.btrfs_ops import BtrfsOps

_ENABLED = (
    os.environ.get("BTRFS_RESTORE_E2E") == "1"
    and os.geteuid() == 0
    and shutil.which("mkfs.btrfs")
    and shutil.which("btrfs")
)


@unittest.skipUnless(_ENABLED, "needs root + mkfs.btrfs + BTRFS_RESTORE_E2E=1")
class TestBtrfsRoundTrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workdir = Path(tempfile.mkdtemp(prefix="btrfs-e2e-"))
        cls.img = cls.workdir / "fs.img"
        cls.mnt = cls.workdir / "mnt"
        cls.mnt.mkdir()
        subprocess.run(["truncate", "-s", "512M", str(cls.img)], check=True)
        subprocess.run(["mkfs.btrfs", "-q", str(cls.img)], check=True)
        subprocess.run(["mount", "-o", "loop", str(cls.img), str(cls.mnt)], check=True)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["umount", str(cls.mnt)], capture_output=True)
        shutil.rmtree(cls.workdir, ignore_errors=True)

    def _subvol(self, name: str) -> Path:
        p = self.mnt / name
        subprocess.run(["btrfs", "subvolume", "create", str(p)], check=True,
                       capture_output=True)
        return p

    def test_full_then_incremental_send_receive(self):
        ops = BtrfsOps()
        src = self._subvol("src")
        (src / "keep.txt").write_text("hello\n")
        (src / "sub").mkdir()
        (src / "sub" / "nested.txt").write_text("one\n")

        # -- full: snapshot -> send -> receive --
        snap1 = self.mnt / "snap1"
        ops.snapshot_ro(src, snap1)
        recv1 = self._as_dir("recv1")
        self.assertEqual(ops.send_local_receive(snap1, None, recv1), 0)
        got = recv1 / "snap1"
        self.assertTrue((got / "keep.txt").is_file())
        self.assertEqual((got / "keep.txt").read_text(), "hello\n")
        self.assertEqual((got / "sub" / "nested.txt").read_text(), "one\n")

        # -- incremental: change src, snapshot, send -p snap1 --
        (src / "added.txt").write_text("brand new\n")
        (src / "keep.txt").write_text("hello again\n")
        snap2 = self.mnt / "snap2"
        ops.snapshot_ro(src, snap2)
        recv2 = self._as_dir("recv2")
        # the parent must be present on the receiving side
        ops.send_local_receive(snap1, None, recv2)
        self.assertEqual(ops.send_local_receive(snap2, snap1, recv2), 0)
        got2 = recv2 / "snap2"
        self.assertEqual((got2 / "keep.txt").read_text(), "hello again\n")
        self.assertEqual((got2 / "added.txt").read_text(), "brand new\n")

        for sv in ("snap1", "snap2", "src"):
            subprocess.run(["btrfs", "subvolume", "delete", str(self.mnt / sv)],
                           capture_output=True)

    def _as_dir(self, name: str) -> Path:
        p = self.mnt / name
        p.mkdir()
        return p


if __name__ == "__main__":
    unittest.main()
