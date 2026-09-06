"""
Snapshot scanner and detector for local, USB, and remote Btrfs subvolumes and archives.
"""
import os
import re
import getpass
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Optional
import xml.etree.ElementTree as ET

from .config import Config
from .models import SnapshotInfo, SnapshotType


class SnapshotScanner:
    def __init__(self, snapshots_dir: Path = Path("/.snapshots")):
        self.snapshots_dir = snapshots_dir
        self.user = os.getenv("SUDO_USER") or os.getenv("USER") or getpass.getuser()
        self.config = Config.load()

    def scan_local_snapshots(self) -> List[SnapshotInfo]:
        """Scans /.snapshots for backup subvolumes and Snapper snapshots."""
        snapshots: List[SnapshotInfo] = []
        if not self.snapshots_dir.exists():
            return snapshots

        try:
            entries = sorted(self.snapshots_dir.iterdir(), key=lambda p: p.name)
        except PermissionError:
            return snapshots

        for entry in entries:
            # 1. home_parent
            if entry.name == "home_parent":
                user_home_dir = entry / self.user
                target_path = user_home_dir if user_home_dir.exists() else entry
                stat = entry.stat()
                snap_time = max(
                    datetime.fromtimestamp(stat.st_mtime),
                    datetime.fromtimestamp(stat.st_ctime),
                )
                snapshots.append(
                    SnapshotInfo(
                        id="local_home_parent",
                        name="home_parent [User /home]",
                        path=target_path,
                        snap_type=SnapshotType.LOCAL,
                        timestamp=snap_time,
                        description="Local master snapshot of /home (Backup-Now)",
                        is_subvolume=True,
                    )
                )

            # 2. root_parent
            elif entry.name == "root_parent":
                stat = entry.stat()
                snap_time = max(
                    datetime.fromtimestamp(stat.st_mtime),
                    datetime.fromtimestamp(stat.st_ctime),
                )
                snapshots.append(
                    SnapshotInfo(
                        id="local_root_parent",
                        name="root_parent [System /]",
                        path=entry,
                        snap_type=SnapshotType.LOCAL,
                        timestamp=snap_time,
                        description="Local master snapshot of root / (Backup-Now)",
                        is_subvolume=True,
                    )
                )

            # 3. Timestamped local snapshots (e.g. home_20260906_153000, root_20260906_153000)
            elif entry.is_dir() and (entry.name.startswith("home_") or entry.name.startswith("root_")):
                match = re.search(r"(home|root)_(\d{8}_\d{6})", entry.name)
                if match:
                    target_type = "Home" if match.group(1) == "home" else "Root /"
                    try:
                        snap_time = datetime.strptime(match.group(2), "%Y%m%d_%H%M%S")
                    except ValueError:
                        snap_time = datetime.fromtimestamp(entry.stat().st_mtime)

                    target_path = entry
                    if match.group(1) == "home":
                        user_home_dir = entry / self.user
                        if user_home_dir.exists():
                            target_path = user_home_dir

                    snapshots.append(
                        SnapshotInfo(
                            id=f"local_{entry.name}",
                            name=f"Local: {target_type} [{snap_time.strftime('%d-%b %H:%M')}]",
                            path=target_path,
                            snap_type=SnapshotType.LOCAL,
                            timestamp=snap_time,
                            description=f"Local Btrfs snapshot {entry.name}",
                            is_subvolume=True,
                        )
                    )

            # 4. Snapper snapshots
            elif entry.is_dir() and entry.name.isdigit():
                xml_path = entry / "info.xml"
                snap_dir = entry / "snapshot"
                if xml_path.exists() and snap_dir.exists():
                    try:
                        tree = ET.parse(xml_path)
                        root = tree.getroot()
                        date_str = root.findtext("date", "")
                        desc = root.findtext("description", "")
                        num = root.findtext("num", entry.name)

                        try:
                            snap_time = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
                        except ValueError:
                            snap_time = datetime.fromtimestamp(entry.stat().st_mtime)

                        snapshots.append(
                            SnapshotInfo(
                                id=f"snapper_{num}",
                                name=f"Snapper #{num} [{desc or 'System'}]",
                                path=snap_dir,
                                snap_type=SnapshotType.LOCAL,
                                timestamp=snap_time,
                                description=f"Snapper snapshot #{num}: {desc}",
                                is_subvolume=True,
                            )
                        )
                    except Exception:
                        pass

        # Sort local snapshots descending by time (newest first)
        snapshots.sort(key=lambda s: s.timestamp, reverse=True)
        return snapshots

    def scan_usb_snapshots(self) -> List[SnapshotInfo]:
        """Scans mounted USB storage for native Btrfs subvolumes and legacy archives."""
        snapshots: List[SnapshotInfo] = []
        media_base = Path(f"/run/media/{self.user}")
        if not media_base.exists():
            return snapshots

        for drive in media_base.iterdir():
            if not drive.is_dir():
                continue

            # Check for native Btrfs subvolumes in drive/home and drive/root
            for target_kind in ["home", "root"]:
                kind_dir = drive / target_kind
                if kind_dir.exists() and kind_dir.is_dir():
                    try:
                        for entry in sorted(kind_dir.iterdir(), key=lambda p: p.name, reverse=True):
                            if entry.is_dir() and entry.name.startswith(f"{target_kind}_"):
                                match = re.search(rf"{target_kind}_(\d{{8}}_\d{{6}})", entry.name)
                                if match:
                                    try:
                                        parsed_time = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
                                    except ValueError:
                                        parsed_time = datetime.fromtimestamp(entry.stat().st_mtime)
                                else:
                                    parsed_time = datetime.fromtimestamp(entry.stat().st_mtime)

                                target_type = "Home" if target_kind == "home" else "Root /"
                                target_path = entry
                                if target_kind == "home":
                                    user_home = entry / self.user
                                    if user_home.exists():
                                        target_path = user_home

                                snapshots.append(
                                    SnapshotInfo(
                                        id=f"usb_btrfs_{entry.name}",
                                        name=f"USB [{drive.name}]: {target_type} [{parsed_time.strftime('%d-%b %H:%M')}]",
                                        path=target_path,
                                        snap_type=SnapshotType.USB,
                                        timestamp=parsed_time,
                                        description=f"Native Btrfs subvolume on {drive.name}",
                                        is_subvolume=True,
                                    )
                                )
                    except PermissionError:
                        pass

            # Legacy fallback: check for .btrfs.zst flat files in backup directories
            for bdir_name in ["backups", "btrfs_backups", "dellomar_backups"]:
                backup_dir = drive / bdir_name
                if backup_dir.exists() and backup_dir.is_dir():
                    for file in backup_dir.glob("*.btrfs.zst"):
                        mtime = datetime.fromtimestamp(file.stat().st_mtime)
                        match = re.search(r"(?:[a-zA-Z0-9_-]+_)?(root|home)_(\d{8}_\d{6})", file.name)
                        if match:
                            target_type = "Home" if "home" in match.group(1).lower() else "Root /"
                            time_str = match.group(2)
                            try:
                                parsed_time = datetime.strptime(time_str, "%Y%m%d_%H%M%S")
                            except ValueError:
                                parsed_time = mtime
                        else:
                            parsed_time = mtime
                            target_type = "Backup"

                        size = file.stat().st_size
                        size_mb = size / (1024 * 1024)
                        size_str = f"{size_mb:.1f} MB" if size_mb < 1024 else f"{size_mb/1024:.2f} GB"

                        snapshots.append(
                            SnapshotInfo(
                                id=f"usb_{file.stem}",
                                name=f"USB [{drive.name}]: {target_type} [{parsed_time.strftime('%d-%b %H:%M')}] ({size_str})",
                                path=file,
                                snap_type=SnapshotType.USB,
                                timestamp=parsed_time,
                                description=f"Standalone backup archive in {drive.name}",
                                is_subvolume=False,
                                size_bytes=size,
                            )
                        )

        snapshots.sort(key=lambda s: s.timestamp, reverse=True)
        return snapshots

    def scan_remote_snapshots(self) -> List[SnapshotInfo]:
        """Scans remote native Btrfs subvolumes and archives on configured remote server via SSH."""
        snapshots: List[SnapshotInfo] = []
        ip = self.config.remote_host
        if not ip:
            return snapshots

        remote_dest = self.config.remote_path
        remote_user = self.config.remote_user or self.user
        remote_name = self.config.remote_name or "Remote"
        remote_port = self.config.remote_port

        ssh_key = Path(f"/home/{self.user}/.ssh/id_ed25519")
        known_hosts = Path(f"/home/{self.user}/.ssh/known_hosts")

        cmd = ["ssh"]
        if remote_port != 22:
            cmd.extend(["-p", str(remote_port)])
        if ssh_key.exists():
            cmd.extend(["-i", str(ssh_key)])
        if known_hosts.exists():
            cmd.extend(["-o", f"UserKnownHostsFile={known_hosts}"])

        # Query native subvolumes in remote backup destination
        remote_cmd = (
            f"sudo btrfs subvolume list {remote_dest} 2>/dev/null; "
            f"ls -l --time-style=+%Y-%m-%d\\ %H:%M:%S {remote_dest}/*.btrfs.zst 2>/dev/null || true"
        )
        cmd.extend([
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=5",
            "-o", "BatchMode=yes",
            f"{remote_user}@{ip}",
            remote_cmd,
        ])
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=16)
            if res.returncode != 0:
                return snapshots
        except (subprocess.SubprocessError, OSError):
            return snapshots

        for line in res.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue

            # Case A: btrfs subvolume list output
            if "path " in line:
                sub_path = line.split("path ", 1)[1].strip()
                snap_name = sub_path.split("/")[-1]
                if not (snap_name.startswith("home_") or snap_name.startswith("root_")):
                    continue

                target_kind = "Home" if sub_path.startswith("home") else "Root /"
                match = re.search(r"(\d{8}_\d{6})", snap_name)
                if match:
                    try:
                        parsed_time = datetime.strptime(match.group(1), "%Y%m%d_%H%M%S")
                    except ValueError:
                        parsed_time = datetime.now()
                else:
                    parsed_time = datetime.now()

                snapshots.append(
                    SnapshotInfo(
                        id=f"remote_{snap_name}",
                        name=f"{remote_name}: {target_kind} [{parsed_time.strftime('%d-%b %H:%M')}]",
                        path=Path(f"{remote_dest}/{sub_path}"),
                        snap_type=SnapshotType.REMOTE,
                        timestamp=parsed_time,
                        description=f"Native remote Btrfs subvolume on {remote_name} ({sub_path})",
                        is_subvolume=True,
                    )
                )

            # Case B: legacy flat .btrfs.zst files
            elif line.endswith(".btrfs.zst"):
                parts = line.split()
                if len(parts) >= 7:
                    try:
                        size = int(parts[4])
                        filename = parts[7].split('/')[-1]
                        match = re.search(r"(?:[a-zA-Z0-9_-]+_)?(root|home)_(\d{8}_\d{6})", filename)
                        if match:
                            target_type = "Home" if "home" in match.group(1).lower() else "Root /"
                            time_str = match.group(2)
                            parsed_time = datetime.strptime(time_str, "%Y%m%d_%H%M%S")
                        else:
                            target_type = "Remote"
                            parsed_time = datetime.strptime(f"{parts[5]} {parts[6]}", "%Y-%m-%d %H:%M:%S")

                        is_full = "_full" in filename or "033102" in filename
                        full_tag = " [Full]" if is_full else ""
                        snap_id = f"remote_{filename.replace('.', '_')}"
                        size_mb = size / (1024 * 1024)
                        size_str = f"{size_mb:.1f} MB" if size_mb < 1024 else f"{size_mb/1024:.2f} GB"

                        snapshots.append(
                            SnapshotInfo(
                                id=snap_id,
                                name=f"{remote_name}: {target_type}{full_tag} [{parsed_time.strftime('%d-%b %H:%M')}] ({size_str})",
                                path=Path(f"{remote_dest}/{filename}"),
                                snap_type=SnapshotType.REMOTE,
                                timestamp=parsed_time,
                                description=f"Remote snapshot stream on {remote_name} ({filename})",
                                is_subvolume=False,
                                size_bytes=size,
                            )
                        )
                    except Exception:
                        continue

        snapshots.sort(key=lambda s: s.timestamp, reverse=True)
        return snapshots

    # Alias for backwards compatibility
    scan_i7server_snapshots = scan_remote_snapshots

    def scan_all(self) -> List[SnapshotInfo]:
        """Returns all available snapshots (Local + USB + Remote)."""
        all_snaps = []
        all_snaps.extend(self.scan_local_snapshots())
        all_snaps.extend(self.scan_usb_snapshots())
        all_snaps.extend(self.scan_remote_snapshots())
        return all_snaps
