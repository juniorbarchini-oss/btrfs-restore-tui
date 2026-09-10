import atexit
import errno
import logging
import os
import re
import shutil
import signal
import getpass
import subprocess
import threading
from pathlib import Path
from typing import Callable, Generator, List, Optional

from .config import Config, _fstype_of
from .models import ConflictResolution, RestoreItem, RestoreProgress, SnapshotInfo, SnapshotType

logger = logging.getLogger("btrfs_restore")

_DEFAULT_STAGING = Path("/.snapshots/staging")
# read-only SSHFS mounts of remote snapshots live here (sibling of staging), one
# "<pid>-<slug>/" dir per run, lazy-unmounted + removed on start / cancel / exit.
_REMOTE_MNT_DIRNAME = ".remote-mnt"
# staging run dir name: "<pid>-<slug>"
_STAGING_RUN_RE = re.compile(r"^(\d+)-")

# pv between the producer and `btrfs receive` gives a live byte counter + rate +
# elapsed even though `btrfs send` reports nothing itself. `-f` forces output
# off a tty; the stream has no known total size so pv cannot show an ETA.
_PV_ARGS = ["pv", "-f", "-b", "-t", "-r", "-i", "1"]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _FatalRestore(RuntimeError):
    """Raised inside the rsync helper for an unrecoverable condition (disk full)
    so restore_generator can abort the whole run with fatal=True."""


# out-format fields split on the unit separator (never valid in a path component)
_RSYNC_SEP = "\x1f"
_RSYNC_FMT = f">>%i{_RSYNC_SEP}%l{_RSYNC_SEP}%n"


class RestoreEngine:
    def __init__(self, progress_cb: Optional[Callable[[str], None]] = None):
        self._active_procs: List[subprocess.Popen] = []
        self._progress_cb = progress_cb
        self._remote_mounts: List[Path] = []
        self._config = Config.load()
        self.staging_dir = self._config.staging_dir or _DEFAULT_STAGING
        # Determine real user UID and GID (when running under sudo)
        sudo_uid = os.getenv("SUDO_UID")
        sudo_gid = os.getenv("SUDO_GID")

        if sudo_uid and sudo_gid:
            self.target_uid = int(sudo_uid)
            self.target_gid = int(sudo_gid)
        else:
            self.target_uid = os.getuid()
            self.target_gid = os.getgid()

        self._user = os.getenv("SUDO_USER") or os.getenv("USER") or getpass.getuser()
        self._staging_run: Optional[Path] = None

    def cancel_active_operation(self) -> None:
        """Cancel any running streaming process and purge this run's staging /
        remote mount."""
        self._kill_active_procs()
        self._cleanup_own_staging()
        self._cleanup_remote_mounts()

    def _kill_active_procs(self) -> None:
        """SIGTERM every stage's process group, then SIGKILL whatever is still
        alive - so a cancel actually stops a `btrfs send | ... | ssh` transfer
        instead of leaving it running detached."""
        for sig in (signal.SIGTERM, signal.SIGKILL):
            alive = [p for p in self._active_procs if p.poll() is None]
            if not alive:
                break
            for proc in alive:
                try:
                    os.killpg(os.getpgid(proc.pid), sig)
                except (ProcessLookupError, PermissionError, OSError):
                    try:
                        proc.send_signal(sig)
                    except OSError:
                        pass
            for proc in alive:
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    pass
        self._active_procs = []

    def _maybe_pv(self, stages: List[List[str]]) -> tuple:
        """Splice a `pv` stage before the final `btrfs receive` when a progress
        callback is set and `pv` is on PATH. Returns (stages, pv_index) where
        pv_index is -1 when nothing was spliced."""
        if self._progress_cb and shutil.which("pv") and len(stages) >= 2:
            spliced = [*stages[:-1], list(_PV_ARGS), stages[-1]]
            return spliced, len(spliced) - 2
        return list(stages), -1

    def _pump_pv(self, proc: subprocess.Popen) -> None:
        """Feed pv's \\r-delimited stderr lines to the progress callback."""
        buf = b""
        try:
            while True:
                chunk = proc.stderr.read(64)
                if not chunk:
                    break
                buf += chunk
                while b"\r" in buf:
                    line, buf = buf.split(b"\r", 1)
                    text = line.decode("utf-8", "replace").strip()
                    if text and self._progress_cb:
                        self._progress_cb(text)
        except (OSError, ValueError):
            pass

    def _run_pipeline(self, stages: List[List[str]], *, progress: bool = False) -> None:
        """Run `stages[0] | stages[1] | ...` with argv lists (never a shell).
        Each process gets its own session so cancel can killpg the whole tree
        (including a remote ssh). Raises RuntimeError with stderr on failure.
        With `progress=True` a `pv` stage is spliced in (when available) and its
        byte/rate counter is streamed to the progress callback."""
        pv_index = -1
        if progress:
            stages, pv_index = self._maybe_pv(stages)

        procs: List[subprocess.Popen] = []
        prev_stdout = None
        try:
            for i, stage in enumerate(stages):
                p = subprocess.Popen(
                    stage,
                    stdin=prev_stdout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                procs.append(p)
                if prev_stdout is not None:
                    prev_stdout.close()
                prev_stdout = p.stdout
        except FileNotFoundError as exc:
            for p in procs:
                p.kill()
            raise RuntimeError(f"missing command: {exc}") from exc

        self._active_procs = procs
        pv_proc = procs[pv_index] if pv_index >= 0 else None
        pump: Optional[threading.Thread] = None
        if pv_proc is not None:
            pump = threading.Thread(target=self._pump_pv, args=(pv_proc,), daemon=True)
            pump.start()

        errs = []
        try:
            for p in procs:
                if p is pv_proc:
                    p.wait()            # its stderr is drained by the pump thread
                    continue
                _, err = p.communicate()
                if err:
                    errs.append(err.decode("utf-8", "replace").strip())
        except BaseException:
            # Ctrl-C / SIGTERM mid-stream: the stages are in their own sessions
            # and never saw the terminal's SIGINT - kill them here.
            self._kill_active_procs()
            if pump is not None:
                pump.join(timeout=1.0)
            raise
        if pump is not None:
            pump.join(timeout=1.0)
        self._active_procs = []

        failed = next((p for p in procs
                       if p is not pv_proc and p.returncode not in (0, None)), None)
        if failed is not None:
            msg = " | ".join(e for e in errs if e) or "stream failed"
            low = msg.lower()
            if "parent" in low or "cannot find" in low or "no such file" in low:
                raise RuntimeError(
                    "Cannot receive incremental delta: parent subvolume missing "
                    "on local disk. This backup requires its base parent.")
            raise RuntimeError(msg)

    def _within_user_home(self, p: Path) -> bool:
        """True when `p` is the invoking user's home or something under it."""
        try:
            home = Path(f"/home/{self._user}")
            p = Path(p).resolve() if p.exists() else Path(p)
            return p == home or home in p.parents
        except (OSError, ValueError):
            return False

    def _apply_ownership(self, path: Path, *, to_user: bool,
                         src: Optional[Path] = None) -> None:
        """Set ownership on a restored path.

        Restoring into the user's home -> give it to the user (files copied as
        root otherwise). Restoring to `/` or another system path -> keep the
        owner recorded in the snapshot (`src`), so e.g. /etc/sudoers stays
        root:root; if `src` is unknown, leave it as created (root)."""
        try:
            if to_user:
                os.chown(path, self.target_uid, self.target_gid, follow_symlinks=False)
            elif src is not None:
                st = src.stat(follow_symlinks=False)
                os.chown(path, st.st_uid, st.st_gid, follow_symlinks=False)
        except (PermissionError, ProcessLookupError, OSError, FileNotFoundError):
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

    # -- fast directory copy via rsync -------------------------------------
    #
    # A per-file Python copy loop is slow for a big tree and does not carry
    # ACLs, xattrs, hardlinks or sparse regions. rsync does all of that in one
    # process; we parse its --out-format stream to keep the progress bar live.

    def _rsync_argv(self, src_dir: Path, dest_dir: Path,
                    resolution: ConflictResolution, to_user: bool) -> List[str]:
        root = os.geteuid() == 0
        argv = ["rsync", "-rlptD", "--out-format=" + _RSYNC_FMT]
        if root:
            # -A/-X (ACLs, xattrs) and owner/group only make sense as root and
            # can hard-error on a filesystem that lacks them otherwise.
            argv += ["-AXH", "-S", "-go", "--numeric-ids"]
            if to_user:
                argv.append(f"--chown={self.target_uid}:{self.target_gid}")
        if resolution == ConflictResolution.SKIP:
            argv.append("--ignore-existing")
        elif resolution == ConflictResolution.BACKUP:
            argv += ["--backup", "--suffix=.bak"]
        # OVERWRITE == rsync default
        argv += ["--", f"{src_dir}/", f"{dest_dir}/"]
        return argv

    def _rsync_item(self, src_dir: Path, dest_dir: Path,
                    resolution: ConflictResolution, to_user: bool):
        """Copy `src_dir` into `dest_dir` with rsync. Yields
        ('progress', n_files, n_bytes, last_name) as work lands and
        ('error', message) for a partial-transfer failure. Raises _FatalRestore
        on ENOSPC."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        argv = self._rsync_argv(src_dir, dest_dir, resolution, to_user)
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        self._active_procs = [proc]
        n_files = n_bytes = 0
        last_name = ""
        try:
            for line in proc.stdout:
                if not line.startswith(">>"):
                    continue
                try:
                    itemize, length, name = line[2:].rstrip("\n").split(_RSYNC_SEP, 2)
                except ValueError:
                    continue
                if len(itemize) > 1 and itemize[1] in ("f", "L"):   # regular file / symlink
                    n_files += 1
                    try:
                        n_bytes += int(length)
                    except ValueError:
                        pass
                    last_name = name.rsplit("/", 1)[-1]
                    if n_files >= 64:
                        yield ("progress", n_files, n_bytes, last_name)
                        n_files = n_bytes = 0
            proc.wait()
        finally:
            self._active_procs = []
        if n_files:
            yield ("progress", n_files, n_bytes, last_name)

        err = (proc.stderr.read() or "").strip()
        rc = proc.returncode
        if rc in (0, 24):          # 24 == source files vanished during the run
            return
        if rc == 11 or "No space left" in err:
            raise _FatalRestore(f"Disk full while restoring {dest_dir.name}")
        for eline in [e for e in err.splitlines() if e][:8]:
            yield ("error", eline)
        if rc != 23:               # 23 == partial transfer (some files failed)
            yield ("error", f"rsync exited {rc} for {src_dir.name}")

    def restore_generator(
        self,
        items: List[RestoreItem],
        target_base: Path,
        resolution: ConflictResolution = ConflictResolution.BACKUP,
        preserve_system_ownership: Optional[bool] = None,
    ) -> Generator[RestoreProgress, None, None]:
        """
        Execute file restoration incrementally, yielding progress states
        to drive the ASCII spinner and progress bar.

        A per-file error (permission, a vanished source) is collected and the
        run continues; the final state carries `done=True` together with an
        `error` summary and `failed_files` - never a bare "completed". A fatal
        error (target not writable, disk full) aborts at once with
        `done=True, fatal=True`.

        `preserve_system_ownership`: None -> decide from the target (user home =
        chown to user, anywhere else = keep the snapshot's owner). True/False
        forces it.
        """
        target_base = Path(target_base)
        if preserve_system_ownership is None:
            to_user = self._within_user_home(target_base)
        else:
            to_user = not preserve_system_ownership

        total_bytes = sum(item.size_bytes for item in items)

        def _state(**kw):
            base = dict(total_files=total_files, processed_files=processed_files,
                        total_bytes=total_bytes, processed_bytes=processed_bytes,
                        current_file="", spinner_idx=spinner_frame,
                        failed_files=len(errors), errors=list(errors))
            base.update(kw)
            return RestoreProgress(**base)

        total_files = 0
        processed_files = 0
        processed_bytes = 0
        spinner_frame = 0
        errors: List[str] = []

        # -- fatal preflight: is the target base usable at all? --
        try:
            target_base.mkdir(parents=True, exist_ok=True)
            probe = target_base / ".btrfs-restore-write-test"
            probe.write_text("ok")
            probe.unlink()
        except OSError as exc:
            yield _state(current_file="", done=True, fatal=True,
                         error=f"Cannot write to {target_base}: {exc}")
            return

        use_rsync = shutil.which("rsync") is not None
        dir_items = [it for it in items if it.is_dir]

        all_files_to_copy = []
        dirs_to_make = []          # (src_dir, rel) - recreated even when empty
        for item in items:
            if item.is_dir:
                dirs_to_make.append((item.source_path, item.rel_path))
                for root, subdirs, files in os.walk(item.source_path):
                    try:
                        base_rel = Path(root).relative_to(item.source_path)
                    except ValueError:
                        continue
                    for d in subdirs:
                        dirs_to_make.append((Path(root) / d, item.rel_path / base_rel / d))
                    for f in files:
                        all_files_to_copy.append(
                            (Path(root) / f, item.rel_path / base_rel / f))
            else:
                all_files_to_copy.append((item.source_path, item.rel_path))

        total_files = len(all_files_to_copy)
        yield _state(current_file="Preparing...")

        # -- recreate the directory tree first, so empty dirs survive a restore
        #    and every level gets the right owner (not just each file's parent) --
        for src_dir, rel in dirs_to_make:
            dest_dir = target_base / rel
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                self._apply_ownership(dest_dir, to_user=to_user, src=src_dir)
            except OSError as exc:
                errors.append(f"{rel}/: {exc.strerror or exc}")

        # -- directory selections: one rsync each (fast, carries ACLs / xattrs /
        #    hardlinks / sparse regions). Standalone files stay on the per-file
        #    path below so its conflict + error handling is unchanged. --
        if use_rsync and dir_items:
            for item in dir_items:
                dest_dir = target_base / item.rel_path
                try:
                    for ev in self._rsync_item(item.source_path, dest_dir,
                                               resolution, to_user):
                        if ev[0] == "progress":
                            _, n_files, n_bytes, name = ev
                            processed_files += n_files
                            processed_bytes += n_bytes
                            spinner_frame = (spinner_frame + 1) % 4
                            yield _state(current_file=name)
                        else:
                            errors.append(f"{item.rel_path}: {ev[1]}")
                            yield _state(current_file=f"! {item.rel_path.name}")
                except _FatalRestore as exc:
                    yield _state(current_file="", done=True, fatal=True,
                                 error=f"{exc} - aborted with "
                                       f"{processed_files}/{total_files} done")
                    return
            files_to_process = [(it.source_path, it.rel_path)
                                for it in items if not it.is_dir]
        else:
            files_to_process = all_files_to_copy

        for src, rel in files_to_process:
            dest = target_base / rel
            try:
                # -- existing-file conflict --
                if dest.exists() or dest.is_symlink():
                    if resolution == ConflictResolution.SKIP:
                        processed_files += 1
                        if src.exists() and not src.is_symlink():
                            processed_bytes += src.stat().st_size
                        continue
                    if resolution == ConflictResolution.BACKUP:
                        bak_path = self._get_backup_path(dest)
                        shutil.move(str(dest), str(bak_path))
                        if to_user:
                            self._apply_ownership(bak_path, to_user=True)
                    elif resolution == ConflictResolution.OVERWRITE:
                        if dest.is_dir() and not dest.is_symlink():
                            shutil.rmtree(dest, ignore_errors=True)
                        else:
                            dest.unlink()

                dest.parent.mkdir(parents=True, exist_ok=True)
                self._apply_ownership(dest.parent, to_user=to_user, src=src.parent)

                # -- copy --
                if src.is_symlink():
                    link_target = os.readlink(src)
                    if dest.exists() or dest.is_symlink():
                        dest.unlink()
                    os.symlink(link_target, dest)
                    file_size = 0
                else:
                    shutil.copy2(src, dest)
                    file_size = src.stat().st_size

                self._apply_ownership(dest, to_user=to_user, src=src)
                processed_bytes += file_size
                processed_files += 1
                spinner_frame = (spinner_frame + 1) % 4
                yield _state(current_file=rel.name)

            except OSError as exc:
                if exc.errno == errno.ENOSPC:
                    errors.append(f"{rel}: disk full")
                    yield _state(current_file=rel.name, done=True, fatal=True,
                                 error=f"Disk full while restoring {rel.name} - "
                                       f"aborted with {processed_files}/{total_files} done")
                    return
                errors.append(f"{rel}: {exc.strerror or exc}")
                yield _state(current_file=f"! {rel.name}")
            except Exception as exc:  # noqa: BLE001 - report, don't crash the run
                errors.append(f"{rel}: {exc}")
                yield _state(current_file=f"! {rel.name}")

        if errors:
            shown = "\n".join(errors[:8])
            if len(errors) > 8:
                shown += f"\n... and {len(errors) - 8} more"
            yield _state(current_file="Completed with errors", done=True,
                         error=f"{len(errors)}/{total_files} file(s) failed:\n{shown}")
        else:
            # rsync's transferred-file count can differ slightly from the walk
            # count (unchanged files, dir entries); land the bar at 100%.
            processed_files = max(processed_files, total_files)
            processed_bytes = max(processed_bytes, total_bytes)
            yield _state(current_file="Completed", done=True)

    def _resolve_staging_dir(self) -> Path:
        """A writable btrfs dir to `btrfs receive` into. Order: configured
        STAGING_DIR, then /.snapshots/staging, then a dir at the top of /'s
        subvolume. Raises a clear error if none of them is on btrfs."""
        candidates: List[Path] = []
        if self._config.staging_dir:
            candidates.append(self._config.staging_dir)
        candidates += [_DEFAULT_STAGING, Path("/.btrfs-restore-staging")]
        for cand in candidates:
            base = cand
            while not base.exists() and base != base.parent:
                base = base.parent
            if _fstype_of(base) == "btrfs":
                return cand
        raise RuntimeError(
            "No btrfs filesystem found for restore staging. Set STAGING_DIR in "
            "~/.config/btrfs-restore/config.conf to a path on a btrfs mount "
            "(the snapshot stream is received there before you browse it).")

    def _purge_staging_path(self, p: Path) -> None:
        """Delete a btrfs subvolume at `p`, or if `p` is a plain dir recurse into
        it (a `<pid>-<slug>/` run dir holds the received subvolume) and remove
        the dir itself."""
        try:
            r = subprocess.run(["btrfs", "subvolume", "delete", str(p)],
                               capture_output=True, timeout=15)
            if r.returncode == 0:
                return
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("could not delete staging subvolume %s: %s", p, exc)
        if p.is_dir() and not p.is_symlink():
            for child in list(p.iterdir()):
                self._purge_staging_path(child)
            try:
                p.rmdir()
            except OSError:
                shutil.rmtree(p, ignore_errors=True)

    def cleanup_staging(self, *, orphans_only: bool = False) -> None:
        """Purge staging run dirs. `orphans_only` keeps dirs whose pid is still
        running (a parallel backup/restore) - used on startup."""
        if orphans_only:
            self._sweep_orphan_mounts()
        if not self.staging_dir.exists():
            return
        for entry in self.staging_dir.iterdir():
            if orphans_only:
                m = _STAGING_RUN_RE.match(entry.name)
                if m and _pid_alive(int(m.group(1))):
                    continue
            self._purge_staging_path(entry)

    def _cleanup_own_staging(self) -> None:
        run = self._staging_run
        if run is not None and run.exists():
            self._purge_staging_path(run)
        self._staging_run = None

    # -- lightweight remote browse: read-only SSHFS mount ------------------
    #
    # Restoring a note or a config from i7server should not stream the whole
    # ~30 GB subvolume. We mount the remote snapshot read-only over SSHFS and
    # let the restore engine read only the files the user actually marked
    # (rsync / copy pull them across on demand). deploy_staging()'s full
    # `btrfs send` stream stays as the fallback for "I lost the whole home".

    def _remote_mnt_base(self) -> Path:
        stg = self._config.staging_dir or _DEFAULT_STAGING
        return stg.parent / _REMOTE_MNT_DIRNAME

    def _sshfs_argv(self, snapshot: SnapshotInfo, config: Config,
                    user: str, mountpoint: Path) -> List[str]:
        """`sshfs <ruser>@<host>:<path> <mnt>` + options, all as argv elements -
        the remote path is never spliced into a shell string."""
        remote_user = config.remote_user or user
        src = f"{remote_user}@{config.remote_host}:{snapshot.path}"
        argv = ["sshfs", src, str(mountpoint),
                "-o", "ro",
                "-o", "reconnect",
                "-o", "ConnectTimeout=10",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "BatchMode=yes",
                "-o", "compression=yes"]
        if config.remote_port and config.remote_port != 22:
            argv += ["-p", str(config.remote_port)]
        key = Path(f"/home/{user}/.ssh/id_ed25519")
        if key.exists():
            argv += ["-o", f"IdentityFile={key}"]
        kh = Path(f"/home/{user}/.ssh/known_hosts")
        if kh.exists():
            argv += ["-o", f"UserKnownHostsFile={kh}"]
        return argv

    def mount_remote_snapshot(self, snapshot: SnapshotInfo) -> Optional[Path]:
        """Read-only SSHFS mount of a remote subvolume, returning the browsable
        path (descended into the user's home dir for a `home` subvolume). Returns
        None when it is not applicable or fails - the caller then falls back to
        deploy_staging()'s full stream."""
        if shutil.which("sshfs") is None:
            return None
        if (snapshot.snap_type != SnapshotType.REMOTE
                or not snapshot.is_subvolume):
            return None
        config = Config.load()
        if not config.remote_host:
            return None
        user = os.getenv("SUDO_USER") or os.getenv("USER") or getpass.getuser()

        self._sweep_orphan_mounts()
        base = self._remote_mnt_base()
        base.mkdir(parents=True, exist_ok=True)
        os.chmod(base, 0o755)
        slug = re.sub(r"[^A-Za-z0-9_.-]", "_", snapshot.id or snapshot.name)[:48]
        mnt = base / f"{os.getpid()}-{slug}"
        mnt.mkdir(parents=True, exist_ok=True)

        argv = self._sshfs_argv(snapshot, config, user, mnt)
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("sshfs mount failed: %s", exc)
            self._rmdir_quiet(mnt)
            return None
        if r.returncode != 0 or not os.path.ismount(mnt):
            logger.warning("sshfs mount of %s failed: %s", snapshot.path,
                           (r.stderr or "").strip() or f"exit {r.returncode}")
            self._unmount_quiet(mnt)
            self._rmdir_quiet(mnt)
            return None

        self._remote_mounts.append(mnt)
        atexit.register(self._cleanup_remote_mounts)
        logger.info("mounted %s read-only at %s", snapshot.path, mnt)

        user_home = mnt / user
        return user_home if user_home.is_dir() else mnt

    def _unmount_quiet(self, mnt: Path, *, lazy: bool = False) -> None:
        flag = "-uz" if lazy else "-u"
        attempts = [["fusermount3", flag, str(mnt)],
                    ["fusermount", flag, str(mnt)],
                    ["umount"] + (["-l"] if lazy else []) + [str(mnt)]]
        for cmd in attempts:
            try:
                if subprocess.run(cmd, capture_output=True,
                                  timeout=15).returncode == 0:
                    return
            except (OSError, subprocess.SubprocessError):
                continue

    def _rmdir_quiet(self, p: Path) -> None:
        try:
            p.rmdir()
        except OSError:
            pass

    def _cleanup_remote_mounts(self) -> None:
        for mnt in list(self._remote_mounts):
            self._unmount_quiet(mnt)
            self._rmdir_quiet(mnt)
        self._remote_mounts = []
        base = self._remote_mnt_base()
        if base.is_dir():
            self._rmdir_quiet(base)

    def _sweep_orphan_mounts(self) -> None:
        """Lazy-unmount + remove remote-mount dirs left by dead runs (startup).
        A dir whose pid is still alive (a parallel restore) is left alone."""
        base = self._remote_mnt_base()
        if not base.is_dir():
            return
        for entry in base.iterdir():
            m = _STAGING_RUN_RE.match(entry.name)
            if m and _pid_alive(int(m.group(1))):
                continue
            self._unmount_quiet(entry, lazy=True)
            self._rmdir_quiet(entry)

    def _ssh_argv(self, config: Config, user: str) -> List[str]:
        """SSH options as a list - no shell string interpolation."""
        argv = ["ssh", "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=10", "-o", "BatchMode=yes"]
        if config.remote_port and config.remote_port != 22:
            argv += ["-p", str(config.remote_port)]
        key = Path(f"/home/{user}/.ssh/id_ed25519")
        if key.exists():
            argv += ["-i", str(key)]
        kh = Path(f"/home/{user}/.ssh/known_hosts")
        if kh.exists():
            argv += ["-o", f"UserKnownHostsFile={kh}"]
        return argv

    def _staging_stages(self, snapshot: SnapshotInfo, config: Config,
                        user: str, receive_into: Optional[Path] = None
                        ) -> Optional[List[List[str]]]:
        """Argv-list pipeline to stage `snapshot`, or None if it needs no staging
        (a mounted subvolume). Paths and config values are always single argv
        elements - never spliced into a shell string."""
        receive = ["btrfs", "receive", str(receive_into or self.staging_dir)]
        if snapshot.snap_type == SnapshotType.REMOTE:
            if not config.remote_host:
                raise RuntimeError("Remote host is not configured "
                                   "(REMOTE_HOST in ~/.config/restore-tui/config.conf)")
            ssh = self._ssh_argv(config, user)
            remote = f"{config.remote_user or user}@{config.remote_host}"
            src = str(snapshot.path)
            if snapshot.is_subvolume:
                return [[*ssh, remote, "sudo", "btrfs", "send", src], receive]
            return [[*ssh, remote, "cat", src], ["zstd", "-dc"], receive]
        if snapshot.snap_type == SnapshotType.USB:
            return [["zstd", "-dc", str(snapshot.path)], receive]
        return None

    def deploy_staging(self, snapshot: SnapshotInfo,
                       progress_cb: Optional[Callable[[str], None]] = None) -> Path:
        """Stream a remote or USB-stream snapshot into the staging dir and return
        the browsable path. A snapshot that is already a mounted subvolume needs
        no staging and is returned directly. `progress_cb` receives pv's
        byte/rate/elapsed line while the stream is received."""
        if progress_cb is not None:
            self._progress_cb = progress_cb
        if snapshot.is_subvolume and snapshot.snap_type == SnapshotType.USB:
            return snapshot.path

        self.staging_dir = self._resolve_staging_dir()   # validated btrfs target
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.staging_dir, 0o755)
        self.cleanup_staging(orphans_only=True)   # clear dead runs, keep parallel ones
        self._cleanup_own_staging()               # drop our previous deploy, if any

        config = Config.load()
        user = os.getenv("SUDO_USER") or os.getenv("USER") or getpass.getuser()

        slug = re.sub(r"[^A-Za-z0-9_.-]", "_", snapshot.id or snapshot.name)[:48]
        run_dir = self.staging_dir / f"{os.getpid()}-{slug}"
        run_dir.mkdir(parents=True, exist_ok=True)
        self._staging_run = run_dir
        atexit.register(self._cleanup_own_staging)

        stages = self._staging_stages(snapshot, config, user, run_dir)
        if stages is None:  # LOCAL - always a mounted subvolume
            self._cleanup_own_staging()
            return snapshot.path

        self._run_pipeline(stages, progress=True)

        for item in run_dir.iterdir():
            if item.is_dir():
                user_home = item / user
                return user_home if user_home.exists() else item

        raise RuntimeError(f"No received subvolume found in {run_dir}")
