import os
import shutil
import signal
import getpass
import subprocess
from pathlib import Path
from typing import Callable, Generator, List, Optional

from .config import Config
from .models import ConflictResolution, RestoreItem, RestoreProgress, SnapshotInfo, SnapshotType


class RestoreEngine:
    def __init__(self):
        self._active_proc: Optional[subprocess.Popen] = None
        # Determine real user UID and GID (when running under sudo)
        sudo_uid = os.getenv("SUDO_UID")
        sudo_gid = os.getenv("SUDO_GID")

        if sudo_uid and sudo_gid:
            self.target_uid = int(sudo_uid)
            self.target_gid = int(sudo_gid)
        else:
            self.target_uid = os.getuid()
            self.target_gid = os.getgid()

    def cancel_active_operation(self) -> None:
        """Cancel any running streaming process and purge staging subvolumes."""
        if self._active_proc and self._active_proc.poll() is None:
            try:
                os.killpg(os.getpgid(self._active_proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    self._active_proc.terminate()
                except Exception:
                    pass
        self._active_proc = None
        self.cleanup_staging()

    def _ensure_ownership(self, path: Path):
        """Ensure file or folder is owned by the real user instead of root."""
        try:
            os.chown(path, self.target_uid, self.target_gid)
        except (PermissionError, ProcessLookupError, OSError):
            pass

    def prepare_items(self, selected_paths: List[Path], snapshot_root: Path) -> List[RestoreItem]:
        """Convert selected paths into RestoreItem list with calculated byte sizes."""
        items: List[RestoreItem] = []

        for p in selected_paths:
            if not p.exists():
                continue

            try:
                rel = p.relative_to(snapshot_root)
            except ValueError:
                rel = Path(p.name)

            if p.is_file() or p.is_symlink():
                size = p.stat().st_size if not p.is_symlink() else 0
                items.append(RestoreItem(source_path=p, is_dir=False, size_bytes=size, rel_path=rel))
            elif p.is_dir():
                total_size = 0
                for root, _, files in os.walk(p):
                    for f in files:
                        fp = Path(root) / f
                        try:
                            if not fp.is_symlink():
                                total_size += fp.stat().st_size
                        except (OSError, PermissionError):
                            pass
                items.append(RestoreItem(source_path=p, is_dir=True, size_bytes=total_size, rel_path=rel))

        return items

    def _get_backup_path(self, target: Path) -> Path:
        """Generate a non-colliding backup path with .bak extension."""
        bak = target.with_name(f"{target.name}.bak")
        idx = 1
        while bak.exists():
            bak = target.with_name(f"{target.name}.bak.{idx}")
            idx += 1
        return bak

    def restore_generator(
        self,
        items: List[RestoreItem],
        target_base: Path,
        resolution: ConflictResolution = ConflictResolution.BACKUP,
    ) -> Generator[RestoreProgress, None, None]:
        """
        Execute file restoration incrementally, yielding progress states
        to drive the ASCII spinner and progress bar.
        """
        total_bytes = sum(item.size_bytes for item in items)
        all_files_to_copy = []

        for item in items:
            if item.is_dir:
                for root, _, files in os.walk(item.source_path):
                    for f in files:
                        src_f = Path(root) / f
                        try:
                            rel_to_item = src_f.relative_to(item.source_path)
                            dest_rel = item.rel_path / rel_to_item
                            all_files_to_copy.append((src_f, dest_rel))
                        except Exception:
                            continue
            else:
                all_files_to_copy.append((item.source_path, item.rel_path))

        total_files = len(all_files_to_copy)
        processed_files = 0
        processed_bytes = 0
        spinner_frame = 0

        yield RestoreProgress(
            total_files=total_files,
            processed_files=0,
            total_bytes=total_bytes,
            processed_bytes=0,
            current_file="Preparing...",
            spinner_idx=0,
        )

        for src, rel in all_files_to_copy:
            dest = target_base / rel

            # Handle existing conflicts
            if dest.exists() or dest.is_symlink():
                if resolution == ConflictResolution.SKIP:
                    processed_files += 1
                    file_size = src.stat().st_size if src.exists() and not src.is_symlink() else 0
                    processed_bytes += file_size
                    continue
                elif resolution == ConflictResolution.BACKUP:
                    bak_path = self._get_backup_path(dest)
                    try:
                        shutil.move(dest, bak_path)
                        self._ensure_ownership(bak_path)
                    except Exception as e:
                        yield RestoreProgress(
                            total_files=total_files,
                            processed_files=processed_files,
                            total_bytes=total_bytes,
                            processed_bytes=processed_bytes,
                            current_file=rel.name,
                            spinner_idx=spinner_frame,
                            error=f"Backup failed: {e}",
                        )
                        continue
                elif resolution == ConflictResolution.OVERWRITE:
                    if dest.is_dir() and not dest.is_symlink():
                        shutil.rmtree(dest, ignore_errors=True)
                    else:
                        try:
                            dest.unlink()
                        except Exception:
                            pass

            dest.parent.mkdir(parents=True, exist_ok=True)
            self._ensure_ownership(dest.parent)

            try:
                # Copy file preserving permissions and timestamps
                if src.is_symlink():
                    link_target = os.readlink(src)
                    if dest.exists() or dest.is_symlink():
                        dest.unlink()
                    os.symlink(link_target, dest)
                    file_size = 0
                else:
                    shutil.copy2(src, dest)
                    file_size = src.stat().st_size

                self._ensure_ownership(dest)
                processed_bytes += file_size
                processed_files += 1
                spinner_frame = (spinner_frame + 1) % 4

                yield RestoreProgress(
                    total_files=total_files,
                    processed_files=processed_files,
                    total_bytes=total_bytes,
                    processed_bytes=processed_bytes,
                    current_file=rel.name,
                    spinner_idx=spinner_frame,
                )

            except Exception as e:
                yield RestoreProgress(
                    total_files=total_files,
                    processed_files=processed_files,
                    total_bytes=total_bytes,
                    processed_bytes=processed_bytes,
                    current_file=rel.name,
                    spinner_idx=spinner_frame,
                    error=str(e),
                )

        yield RestoreProgress(
            total_files=total_files,
            processed_files=processed_files,
            total_bytes=total_bytes,
            processed_bytes=processed_bytes,
            current_file="Completed",
            spinner_idx=spinner_frame,
            done=True,
        )

    def cleanup_staging(self) -> None:
        """Delete temporary subvolumes in /.snapshots/staging."""
        staging_dir = Path("/.snapshots/staging")
        if not staging_dir.exists():
            return
        for item in staging_dir.iterdir():
            if item.is_dir():
                cmd = ["btrfs", "subvolume", "delete", str(item)]
                try:
                    subprocess.run(cmd, capture_output=True, timeout=10)
                except Exception:
                    pass

    def deploy_staging(self, snapshot: SnapshotInfo) -> Path:
        """Deploy a remote or USB snapshot into /.snapshots/staging."""
        staging_dir = Path("/.snapshots/staging")
        staging_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(staging_dir, 0o755)
        self.cleanup_staging()

        config = Config.load()
        user = os.getenv("SUDO_USER") or os.getenv("USER") or getpass.getuser()
        remote_user = config.remote_user or user
        ip = config.remote_host

        if snapshot.snap_type == SnapshotType.REMOTE:
            if not ip:
                raise RuntimeError("Remote host is not configured in ~/.config/btrfs-restore/config.conf")

            ssh_key = Path(f"/home/{user}/.ssh/id_ed25519")
            known_hosts = Path(f"/home/{user}/.ssh/known_hosts")

            ssh_opts = ""
            if config.remote_port != 22:
                ssh_opts += f" -p {config.remote_port}"
            if ssh_key.exists():
                ssh_opts += f" -i {ssh_key}"
            if known_hosts.exists():
                ssh_opts += f" -o UserKnownHostsFile={known_hosts}"

            if snapshot.is_subvolume:
                # Native remote Btrfs subvolume on remote server
                remote_cmd = (
                    f"ssh -o StrictHostKeyChecking=no{ssh_opts} {remote_user}@{ip} "
                    f"'sudo btrfs send \"{snapshot.path}\"' | btrfs receive '{staging_dir}'"
                )
            else:
                # Legacy .btrfs.zst stream
                remote_cmd = (
                    f"ssh -o StrictHostKeyChecking=no{ssh_opts} {remote_user}@{ip} 'cat \"{snapshot.path}\"' "
                    f"| zstd -dc | btrfs receive '{staging_dir}'"
                )

            self._active_proc = subprocess.Popen(
                remote_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
            )
            stdout, stderr = self._active_proc.communicate()
            ret = self._active_proc.returncode
            self._active_proc = None

            if ret != 0:
                err_msg = (stderr or "").strip()
                if "parent" in err_msg.lower() or "cannot find" in err_msg.lower() or "no such file" in err_msg.lower():
                    raise RuntimeError(
                        "Cannot receive incremental delta: parent subvolume missing on local disk.\n"
                        "This remote backup requires its base parent."
                    )
                raise RuntimeError(err_msg or "Failed to receive remote stream.")

        elif snapshot.snap_type == SnapshotType.USB:
            if snapshot.is_subvolume:
                # If it's already a native subvolume mounted on USB, return directly
                return snapshot.path

            usb_cmd = f"cat '{snapshot.path}' | zstd -dc | btrfs receive '{staging_dir}'"
            self._active_proc = subprocess.Popen(
                usb_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
            )
            stdout, stderr = self._active_proc.communicate()
            ret = self._active_proc.returncode
            self._active_proc = None

            if ret != 0:
                err_msg = (stderr or "").strip()
                if "parent" in err_msg.lower() or "cannot find" in err_msg.lower() or "no such file" in err_msg.lower():
                    raise RuntimeError(
                        "Cannot receive incremental delta: parent subvolume missing on local disk."
                    )
                raise RuntimeError(err_msg or "Failed to receive USB stream.")

        # Find staged subvolume
        for item in staging_dir.iterdir():
            if item.is_dir():
                user_home = item / user
                if user_home.exists():
                    return user_home
                return item

        raise RuntimeError("No received subvolume found in /.snapshots/staging")
