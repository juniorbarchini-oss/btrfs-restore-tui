#!/usr/bin/env python3
"""
Main entry point for Btrfs Restore TUI (AGY Time Explorer).
Automatically elevates privileges with sudo to manage Btrfs operations seamlessly.
"""
import os
import sys
from pathlib import Path

# Add project root to PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.resolve()))


def check_and_elevate():
    """Ensure process is running with root privileges for Btrfs receive and snapshots."""
    if os.geteuid() != 0:
        print("[*] Btrfs Restore TUI requires administrative privileges for Btrfs operations.")
        print("[*] Requesting sudo elevation...")
        # Preserve user environment variables for Wayland/Foot terminal
        args = [
            "sudo",
            "--preserve-env=WAYLAND_DISPLAY,DISPLAY,XDG_RUNTIME_DIR,TERM,SUDO_USER,USER",
            sys.executable,
        ] + sys.argv
        os.execvp("sudo", args)


def main():
    check_and_elevate()
    from btrfs_restore.ui import BtrfsRestoreApp
    app = BtrfsRestoreApp()
    app.run()


if __name__ == "__main__":
    main()
