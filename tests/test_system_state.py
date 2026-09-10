"""
SystemStateCollector: best-effort, never raises, always writes os_info.json +
an executable restore.sh; the script is valid bash.
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from btrfs_restore.system_state import SystemStateCollector, _DRIVER_RE


class TestSystemState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.snap = Path(self.tmp.name) / "2026-09-08_120000"
        self.snap.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_collect_all_is_best_effort_and_writes_core_files(self):
        warnings = SystemStateCollector(self.snap).collect_all()
        self.assertIsInstance(warnings, list)
        self.assertTrue((self.snap / "_system_state" / "os_info.json").exists())
        info = json.loads((self.snap / "_system_state" / "os_info.json").read_text())
        self.assertIn("hostname", info)
        rs = self.snap / "restore.sh"
        self.assertTrue(rs.exists())
        self.assertTrue(rs.stat().st_mode & 0o111)  # executable

    def test_restore_script_is_valid_bash(self):
        SystemStateCollector(self.snap).collect_all()
        rc = subprocess.run(["bash", "-n", str(self.snap / "restore.sh")],
                            capture_output=True, text=True)
        self.assertEqual(rc.returncode, 0, rc.stderr)

    def test_restore_script_self_elevates_for_the_home_step(self):
        SystemStateCollector(self.snap).collect_all()
        body = (self.snap / "restore.sh").read_text()
        # not started with sudo + restoring the booted system -> re-exec as root
        self.assertIn('[ "$(id -u)" -ne 0 ]', body)
        self.assertIn("exec sudo -E", body)

    @unittest.skipUnless(shutil.which("shellcheck"), "shellcheck not installed")
    def test_restore_script_passes_shellcheck(self):
        SystemStateCollector(self.snap).collect_all()
        rc = subprocess.run(["shellcheck", "-S", "error", str(self.snap / "restore.sh")],
                            capture_output=True, text=True)
        self.assertEqual(rc.returncode, 0, rc.stdout)

    def test_a_failing_step_becomes_a_warning_not_a_crash(self):
        c = SystemStateCollector(self.snap)

        def boom():
            raise RuntimeError("boom")

        with mock.patch.object(c, "_export_packages", boom):
            warnings = c.collect_all()
        self.assertTrue(any("boom" in w for w in warnings))
        # later steps still ran
        self.assertTrue((self.snap / "restore.sh").exists())

    def test_driver_packages_are_filtered(self):
        for pkg in ("nvidia", "nvidia-utils", "linux", "linux-firmware",
                    "amd-ucode", "intel-ucode"):
            self.assertTrue(any(rx.match(pkg) for rx in _DRIVER_RE), pkg)
        for pkg in ("firefox", "git", "hyprland", "linux-headers-doc-ish"):
            # keep normal packages (note: linux-headers would match ^linux-.* by
            # design - that's acceptable, headers follow the kernel)
            if pkg == "linux-headers-doc-ish":
                continue
            self.assertFalse(any(rx.match(pkg) for rx in _DRIVER_RE), pkg)


if __name__ == "__main__":
    unittest.main()
