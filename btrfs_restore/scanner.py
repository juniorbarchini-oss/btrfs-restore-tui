"""
Snapshot scanner and detector for local, USB, and remote Btrfs subvolumes and archives.
"""
import json
import logging
import os
import re
import getpass
import shlex
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Optional
import xml.etree.ElementTree as ET

from .config import Config
from .models import SnapshotInfo, SnapshotType

logger = logging.getLogger("btrfs_restore")

_TS_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}(?:_\d)?$")


class SnapshotScanner:
    def __init__(self, snapshots_dir: Path = Path("/.snapshots")):
        self.snapshots_dir = snapshots_dir
        self.user = os.getenv("SUDO_USER") or os.getenv("USER") or getpass.getuser()
        self.config = Config.load()
        # none      - no remote configured
        # unreachable - SSH did not connect (network / auth / host down)
        # no_privilege - connected, but `sudo btrfs` is not allowed there; only
        #                what is readable without root is listed
        # connected  - connected and `sudo btrfs` works
        self.remote_status = "none"
        self.remote_skipped_local = 0

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
                try:
                    stat = entry.stat()
                except (FileNotFoundError, OSError):
                    logger.warning(
                        "Skipping home_parent: symlink is broken or was re-pointed "
                        "by a concurrent backup run"
                    )
                    continue
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
                try:
                    stat = entry.stat()
                except (FileNotFoundError, OSError):
                    logger.warning(
                        "Skipping root_parent: symlink is broken or was re-pointed "
                        "by a concurrent backup run"
                    )
                    continue
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
                    except (ET.ParseError, OSError, ValueError) as exc:
                        logger.warning("skipping snapper snapshot %s: %s", entry.name, exc)

        # Sort local snapshots descending by time (newest first)
        snapshots.sort(key=lambda s: s.timestamp, reverse=True)
        return snapshots

    def scan_target_snapshots(self) -> List[SnapshotInfo]:
        """Read the manifest-based layout written by `backup-now`:

            <target>/btrfs-restore/snapshots/<YYYY-MM-DD_HHMMSS>/
                manifest.json
                root_<ts>/ | root.btrfs.zst
                home_<ts>/ | home.btrfs.zst

        One SnapshotInfo per kind (root/home). Timestamp and status come from
        the manifest, not from parsing names.
        """
        out: List[SnapshotInfo] = []
        base = self.config.snapshots_dir
        if not base or not base.is_dir():
            return out

        try:
            entries = sorted(base.iterdir(), key=lambda p: p.name, reverse=True)
        except OSError as exc:
            logger.warning("cannot list %s: %s", base, exc)
            return out

        for snap in entries:
            if not snap.is_dir() or snap.is_symlink() or not _TS_DIR_RE.match(snap.name):
                continue
            try:
                manifest = json.loads((snap / "manifest.json").read_text())
            except (OSError, ValueError) as exc:
                logger.warning("snapshot %s has no readable manifest: %s", snap.name, exc)
                manifest = {}

            status = manifest.get("status", "unknown")
            if status in ("running", "failed"):
                logger.info("skipping %s snapshot %s", status, snap.name)
                continue

            created = manifest.get("created_at") or manifest.get("started_at")
            try:
                ts = datetime.fromisoformat(created) if created else \
                    datetime.fromtimestamp(snap.stat().st_mtime)
            except (ValueError, OSError):
                ts = datetime.fromtimestamp(snap.stat().st_mtime)

            for kind in ("root", "home"):
                sub = next(iter(sorted(snap.glob(f"{kind}_*"))), None)
                stream = snap / f"{kind}.btrfs.zst"
                if sub and sub.is_dir():
                    path, is_subvol = sub, True
                elif stream.is_file():
                    path, is_subvol = stream, False
                else:
                    continue

                target_path = path
                if kind == "home" and is_subvol:
                    user_home = path / self.user
                    if user_home.exists():
                        target_path = user_home

                label = "Root /" if kind == "root" else "Home"
                tag = "" if status == "completed" else f" ({status})"
                size = manifest.get("size_bytes")
                out.append(SnapshotInfo(
                    id=f"target_{snap.name}_{kind}",
                    name=f"USB: {label} [{ts.strftime('%d-%b %H:%M')}]{tag}",
                    path=target_path,
                    snap_type=SnapshotType.USB,
                    timestamp=ts,
                    description=f"backup-now snapshot {snap.name} ({kind})",
                    is_subvolume=is_subvol,
                    size_bytes=size,
                    status=status,
                    manifest=manifest,
                ))

        out.sort(key=lambda s: s.timestamp, reverse=True)
        return out

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
        """Scans remote native Btrfs subvolumes and archives on configured remote server via SSH.

        Sets self.remote_status: none | unreachable | no_privilege | connected,
        and self.remote_skipped_local = how many remote subvolumes were hidden
        because the same subvolume is already on local disk.
        """
        snapshots: List[SnapshotInfo] = []
        self.remote_status = "none"
        self.remote_skipped_local = 0
        ip = self.config.remote_host
        remote_dest = self.config.remote_path
        if not ip or not remote_dest:
            return snapshots

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

        # One SSH round-trip that also tells us *why* it failed:
        #   __CONN_OK__   - the SSH session itself worked
        #   __SUDO_OK__   - `sudo btrfs` is allowed (full listing follows)
        #   __SUDO_NO__   - connected but no passwordless `sudo btrfs`; fall back
        #                   to a plain `find` of the received-subvolume dirs
        # `sudo -n` fails fast instead of blocking on a password prompt.
        q = shlex.quote
        dest = str(remote_dest).rstrip("/")
        remote_cmd = "; ".join([
            "echo __CONN_OK__",
            f"if sudo -n btrfs subvolume list {q(dest)} 2>/dev/null; then echo __SUDO_OK__; "
            f"else echo __SUDO_NO__; "
            f"find {q(dest)}/root {q(dest)}/home -mindepth 1 -maxdepth 1 "
            f"-printf 'nosudo %p\\n' 2>/dev/null || true; fi",
            f"ls -l --time-style=+%Y-%m-%d\\ %H:%M:%S {q(dest)}/*.btrfs.zst 2>/dev/null || true",
        ])
        cmd.extend([
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=5",
            "-o", "BatchMode=yes",
            f"{remote_user}@{ip}",
            remote_cmd,
        ])
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=16)
        except (subprocess.SubprocessError, OSError) as exc:
            self.remote_status = "unreachable"
            logger.warning("remote scan of %s failed: %s", ip, exc)
            return snapshots

        if res.returncode != 0 or "__CONN_OK__" not in res.stdout:
            self.remote_status = "unreachable"
            logger.warning("remote scan of %s failed: %s", ip,
                           (res.stderr or "").strip() or f"exit {res.returncode}")
            return snapshots

        if "__SUDO_OK__" in res.stdout:
            self.remote_status = "connected"
        else:
            self.remote_status = "no_privilege"
            logger.info("remote %s: connected but no passwordless `sudo btrfs` - "
                        "listing received-subvolume dirs by name only", ip)

        for line in res.stdout.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("__"):
                continue

            # No-privilege fallback: `find` output, one absolute path per line
            if line.startswith("nosudo "):
                full = line[len("nosudo "):].strip()
                snap_name = full.rsplit("/", 1)[-1]
                kind_seg = full.rsplit("/", 2)[-2] if full.count("/") >= 2 else ""
                if kind_seg not in ("root", "home"):
                    continue
                if not (snap_name.startswith("home_") or snap_name.startswith("root_")):
                    continue
                if (self.snapshots_dir / snap_name).is_dir():
                    self.remote_skipped_local += 1
                    continue
                m = re.search(r"(\d{8}_\d{6})", snap_name)
                try:
                    ptime = datetime.strptime(m.group(1), "%Y%m%d_%H%M%S") if m else datetime.now()
                except ValueError:
                    ptime = datetime.now()
                kind_label = "Home" if kind_seg == "home" else "Root /"
                snapshots.append(SnapshotInfo(
                    id=f"remote_{snap_name}",
                    name=f"{remote_name}: {kind_label} [{ptime.strftime('%d-%b %H:%M')}]",
                    path=Path(full),
                    snap_type=SnapshotType.REMOTE,
                    timestamp=ptime,
                    description=f"Remote Btrfs subvolume on {remote_name} ({full})",
                    is_subvolume=True,
                ))
                continue

            # Case A: btrfs subvolume list output
            if "path " in line:
                sub_path = line.split("path ", 1)[1].strip()
                snap_name = sub_path.split("/")[-1]
                if not (snap_name.startswith("home_") or snap_name.startswith("root_")):
                    continue

                # Already on local disk? Don't offer a 30 GB re-download - the
                # local scan lists the very same subvolume.
                if (self.snapshots_dir / snap_name).is_dir():
                    self.remote_skipped_local += 1
                    logger.info("remote %s is already local; not listing it as remote", snap_name)
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
                    except (ValueError, IndexError) as exc:
                        logger.warning("skipping remote entry %r: %s", line, exc)
                        continue

        snapshots.sort(key=lambda s: s.timestamp, reverse=True)
        return snapshots

    def scan_all(self) -> List[SnapshotInfo]:
        """Every available snapshot: manifest-based target + legacy local/USB + remote."""
        all_snaps: List[SnapshotInfo] = []
        all_snaps.extend(self.scan_target_snapshots())
        all_snaps.extend(self.scan_local_snapshots())
        all_snaps.extend(self.scan_usb_snapshots())
        all_snaps.extend(self.scan_remote_snapshots())
        # de-dupe by id, keep first (target layout wins over legacy USB scan)
        seen, deduped = set(), []
        for s in all_snaps:
            if s.id in seen:
                continue
            seen.add(s.id)
            deduped.append(s)
        return deduped
