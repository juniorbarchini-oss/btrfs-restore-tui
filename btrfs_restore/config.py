"""
Configuration loader for Btrfs Restore TUI.
Reads remote backup host settings from environment variables or configuration files.
"""
import os
import getpass
from pathlib import Path
from dataclasses import dataclass


@dataclass
class Config:
    remote_host: str = ""
    remote_path: str = "/mnt/backups"
    remote_user: str = ""
    remote_name: str = "Remote"
    remote_port: int = 22

    @classmethod
    def load(cls) -> "Config":
        user = os.getenv("SUDO_USER") or os.getenv("USER") or getpass.getuser()
        cfg = cls(remote_user=user)

        candidate_paths = [
            Path(f"/home/{user}/.config/btrfs-restore/config.conf"),
            Path.home() / ".config" / "btrfs-restore" / "config.conf",
            Path("/etc/btrfs-restore/config.conf"),
        ]

        for p in candidate_paths:
            if p.is_file():
                try:
                    for line in p.read_text(encoding="utf-8").splitlines():
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        key, val = line.split("=", 1)
                        key = key.strip()
                        val = val.strip().strip('"').strip("'")
                        if key == "BTRFS_REMOTE_HOST":
                            cfg.remote_host = val
                        elif key == "BTRFS_REMOTE_PATH":
                            cfg.remote_path = val
                        elif key == "BTRFS_REMOTE_USER":
                            cfg.remote_user = val
                        elif key == "BTRFS_REMOTE_NAME":
                            cfg.remote_name = val
                        elif key == "BTRFS_REMOTE_PORT":
                            try:
                                cfg.remote_port = int(val)
                            except ValueError:
                                pass
                    break
                except Exception:
                    pass

        # Environment variables take precedence
        if os.getenv("BTRFS_REMOTE_HOST"):
            cfg.remote_host = os.getenv("BTRFS_REMOTE_HOST", "")
        if os.getenv("BTRFS_REMOTE_PATH"):
            cfg.remote_path = os.getenv("BTRFS_REMOTE_PATH", "/mnt/backups")
        if os.getenv("BTRFS_REMOTE_USER"):
            cfg.remote_user = os.getenv("BTRFS_REMOTE_USER", user)
        if os.getenv("BTRFS_REMOTE_NAME"):
            cfg.remote_name = os.getenv("BTRFS_REMOTE_NAME", "Remote")

        if not cfg.remote_name and cfg.remote_host:
            cfg.remote_name = cfg.remote_host

        return cfg
