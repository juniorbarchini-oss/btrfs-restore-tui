"""
Btrfs backup engine: read-only local snapshots + `btrfs send | receive` to a USB
drive or SSH host, incremental when a shared parent exists.

Layout written on the target (identical shape to the ext4 sibling `restore-tui`):

    <target>/btrfs-restore/snapshots/<YYYY-MM-DD_HHMMSS>/
        root_<ts>/          received subvolume        (btrfs target)
        home_<ts>/            ""                        ""
        root.btrfs.zst       compressed send stream    (non-btrfs target)
        home.btrfs.zst        ""                        ""
        _system_state/       os / package lists  (expanded in #2)
        restore.sh           bare-metal recovery (expanded in #2)
        manifest.json        status: completed | partial | failed
        backup.log
    <target>/btrfs-restore/latest -> snapshots/<ts>   (only ever a completed one)

Safety rules (ported from the ext4 engine):
  * A run that cannot finish cleanly is marked partial/failed, `latest` is left
    untouched, and its snapshot is never used as an incremental parent.
  * Local RO snapshots are kept only as `btrfs send -p` parents for the next run;
    the last completed one per kind is never pruned.
"""
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .config import Config
from .btrfs_ops import BtrfsOps, CommandError

logger = logging.getLogger("btrfs_restore")

MANIFEST_VERSION = 1
_SNAPSHOT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}(?:_\d)?$")
_COMPACT_RE = re.compile(r"^([A-Za-z0-9-]+)_\d{8}_\d{6}(?:_\d)?$")

EventCallback = Callable[[str, str], None]  # (event_type, message)


@dataclass
class BackupResult:
    status: str                       # completed | partial | failed
    snapshot_name: str = ""
    snapshot_path: Optional[Path] = None
    parents: Dict[str, Optional[str]] = field(default_factory=dict)
    duration_seconds: float = 0.0
    warnings: List[str] = field(default_factory=list)
    pruned: List[str] = field(default_factory=list)
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "completed"


class BtrfsBackupEngine:
    def __init__(self, cfg: Config, ops: Optional[BtrfsOps] = None,
                 callback: Optional[EventCallback] = None):
        self.cfg = cfg
        self.ops = ops or BtrfsOps()
        self._external_cb = callback or (lambda t, m: None)
        self._logfile = None

    # -- logging ------------------------------------------------------

    def _emit(self, event_type: str, message: str) -> None:
        if self._logfile:
            ts = datetime.now().strftime("%H:%M:%S")
            try:
                self._logfile.write(f"[{ts}] {event_type.upper():8} {message}\n")
                self._logfile.flush()
            except OSError:
                pass
        self._external_cb(event_type, message)

    # -- snapshot / manifest discovery on the target ------------------

    def _iter_target_snapshots(self):
        d = self.cfg.snapshots_dir
        if not d or not d.is_dir():
            return
        for entry in sorted(d.iterdir()):
            if entry.is_dir() and not entry.is_symlink() and _SNAPSHOT_RE.match(entry.name):
                yield entry

    def _read_manifest(self, snap_dir: Path) -> dict:
        try:
            return json.loads((snap_dir / "manifest.json").read_text())
        except (OSError, ValueError):
            return {}

    def latest_completed_manifest(self) -> Optional[dict]:
        best = None
        for s in self._iter_target_snapshots():
            m = self._read_manifest(s)
            if m.get("status") == "completed" and (best is None or s.name > best[0]):
                best = (s.name, m)
        return best[1] if best else None

    # -- manifest write ---------------------------------------------

    def _write_manifest(self, snap_dir: Path, **fields) -> None:
        data = {
            "manifest_version": MANIFEST_VERSION,
            "snapshot": snap_dir.name,
            "hostname": os.uname().nodename,
            "user": self.cfg.user,
            "source_mounts": list(self.cfg.source_mounts),
        }
        data.update(fields)
        tmp = snap_dir / "manifest.json.tmp"
        try:
            tmp.write_text(json.dumps(data, indent=2))
            os.replace(tmp, snap_dir / "manifest.json")
        except OSError as exc:
            self._emit("warning", f"could not write manifest: {exc}")

    # -- main --------------------------------------------------------

    def run(self, dry_run: bool = False) -> BackupResult:
        if not self.cfg.target_root:
            return BackupResult("failed", message=(
                "No backup target. Plug in the USB drive or set TARGET_DIR in "
                "~/.config/restore-tui/config.conf."
            ))
        target_is_btrfs = self.cfg.target_is_btrfs()
        name = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        start = time.time()

        if dry_run:
            return self._dry_run(name, target_is_btrfs)

        if os.geteuid() != 0:
            return BackupResult("failed", message="backup needs root (btrfs snapshot/send)")

        if not self._target_writable():
            return BackupResult("failed", message=(
                f"Target not writable: {self.cfg.target_root} (USB mounted?)"))

        try:
            self.cfg.snapshots_dir.mkdir(parents=True, exist_ok=True)
            snap_dir = self._reserve_snapshot_dir(name)
            name = snap_dir.name
            compact = name.replace("-", "")            # 2026-09-08_101500[_2] -> 20260908_101500[_2]
        except OSError as exc:
            return BackupResult("failed", message=f"cannot create snapshot dir: {exc}")

        self._logfile = open(snap_dir / "backup.log", "w", encoding="utf-8")
        result = BackupResult("failed", snapshot_name=name, snapshot_path=snap_dir)
        prev = self.latest_completed_manifest() or {}
        prev_parents: Dict[str, str] = prev.get("local_snapshots", {})

        try:
            self._write_manifest(snap_dir, status="running",
                                 started_at=datetime.now().isoformat())

            local_snaps: Dict[str, str] = {}
            for mount in self.cfg.source_mounts:
                kind = "root" if mount == "/" else Path(mount).name  # / -> root, /home -> home
                self._emit("stage", f"Snapshot {mount}")
                local = self.cfg.local_snapshots_dir / f"{kind}_{compact}"
                self._make_local_snapshot(Path(mount), local)
                local_snaps[kind] = local.name

                parent = self._pick_parent(kind, prev_parents)
                result.parents[kind] = parent.name if parent else None

                self._emit("stage",
                           f"Send {kind} ({'incremental' if parent else 'full'})")
                self._send(kind, local, parent, snap_dir, target_is_btrfs)

            self._emit("stage", "System state")
            state_warn = self._collect_system_state(snap_dir / "_system_state")
            result.warnings.extend(state_warn)
            for w in state_warn:
                self._emit("warning", w)
            self._write_restore_script(snap_dir, target_is_btrfs)

            duration = round(time.time() - start, 2)
            result.status = "completed"
            result.duration_seconds = duration
            self._write_manifest(
                snap_dir, status="completed",
                created_at=datetime.now().isoformat(),
                duration_seconds=duration,
                target_is_btrfs=bool(target_is_btrfs),
                local_snapshots=local_snaps,
                parents=result.parents,
                warnings=result.warnings,
            )
            self._update_latest(snap_dir)
            self._emit("success", f"Snapshot completed in {duration}s: {name}")
            result.pruned = self._prune(local_snaps)

        except (CommandError, OSError, RuntimeError) as exc:
            result.status = "partial" if isinstance(exc, CommandError) else "failed"
            result.message = str(exc)
            self._emit("error", result.message)
            self._emit("info", "latest pointer left untouched; snapshot won't be a parent")
            try:
                self._write_manifest(snap_dir, status=result.status,
                                     failed_at=datetime.now().isoformat(),
                                     error=result.message)
            except OSError:
                pass
        finally:
            if self._logfile:
                self._logfile.close()
                self._logfile = None

        return result

    # -- steps -----------------------------------------------------

    def _make_local_snapshot(self, mount: Path, dest: Path) -> None:
        if dest.exists():
            try:
                self.ops.delete_subvolume(dest)
            except CommandError:
                shutil.rmtree(dest, ignore_errors=True)
        self.ops.snapshot_ro(mount, dest)

    def _pick_parent(self, kind: str, prev_parents: Dict[str, str]) -> Optional[Path]:
        """The local snapshot recorded by the last completed backup for `kind`,
        if it still exists - that's the only safe `btrfs send -p` base."""
        name = prev_parents.get(kind)
        if not name:
            return None
        p = self.cfg.local_snapshots_dir / name
        return p if p.is_dir() else None

    def _send(self, kind: str, local: Path, parent: Optional[Path],
              snap_dir: Path, target_is_btrfs: Optional[bool]) -> None:
        if target_is_btrfs:
            rc = self.ops.send_local_receive(local, parent, snap_dir)
        else:
            rc = self.ops.send_to_stream(local, parent, snap_dir / f"{kind}.btrfs.zst")
        if rc != 0:
            raise CommandError(rc, f"btrfs send {kind}")

    def _collect_system_state(self, meta_dir: Path) -> List[str]:
        """Minimal for #1 - #2 replaces this with the full Arch collector."""
        warnings: List[str] = []
        meta_dir.mkdir(parents=True, exist_ok=True)
        info = {
            "hostname": os.uname().nodename,
            "kernel": os.uname().release,
            "arch": os.uname().machine,
            "captured_at": datetime.now().isoformat(),
        }
        osr = Path("/etc/os-release")
        if osr.is_file():
            for line in osr.read_text().splitlines():
                if line.startswith("PRETTY_NAME="):
                    info["distro"] = line.split("=", 1)[1].strip().strip('"')
        try:
            (meta_dir / "os_info.json").write_text(json.dumps(info, indent=2))
        except OSError as exc:
            warnings.append(f"os_info: {exc}")
        if shutil.which("pacman"):
            for args, fname in (
                (["pacman", "-Qqe"], "pkglist_explicit.txt"),
                (["pacman", "-Qqm"], "pkglist_aur.txt"),
            ):
                try:
                    import subprocess
                    res = subprocess.run(args, capture_output=True, text=True, timeout=60)
                    if res.returncode == 0:
                        (meta_dir / fname).write_text(res.stdout)
                except (OSError, subprocess.SubprocessError) as exc:
                    warnings.append(f"{fname}: {exc}")
        else:
            warnings.append("pacman not found; package lists not saved (#2)")
        return warnings

    def _write_restore_script(self, snap_dir: Path, target_is_btrfs: Optional[bool]) -> None:
        """Placeholder recovery script - #2 generates the real bare-metal one."""
        script = snap_dir / "restore.sh"
        try:
            script.write_text(
                "#!/usr/bin/env bash\n"
                "# Placeholder - full bare-metal restore.sh is generated in issue #2.\n"
                "echo 'Use restore-now (the TUI) to restore individual files from this snapshot.'\n"
            )
            script.chmod(0o755)
        except OSError as exc:
            self._emit("warning", f"restore.sh: {exc}")

    # -- helpers -----------------------------------------------------

    def _reserve_snapshot_dir(self, name: str) -> Path:
        base = self.cfg.snapshots_dir
        for suffix in ("", *(f"_{n}" for n in range(2, 10))):
            candidate = base / f"{name}{suffix}"
            try:
                candidate.mkdir(parents=False, exist_ok=False)
                return candidate
            except FileExistsError:
                continue
        raise OSError(f"too many snapshots for {name}")

    def _target_writable(self) -> bool:
        root = self.cfg.target_root
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = root / ".btrfs-restore-write-test"
            probe.write_text("ok")
            probe.unlink()
            return True
        except OSError:
            return False

    def _update_latest(self, snap_dir: Path) -> None:
        link = self.cfg.latest_link
        try:
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(Path("snapshots") / snap_dir.name)
        except OSError as exc:
            self._emit("warning", f"could not update 'latest': {exc}")

    def _prune(self, keep_local: Dict[str, str]) -> List[str]:
        pruned: List[str] = []

        # -- target: disk-usage based, never below MIN_KEEP, never the last --
        def _completed():
            c = [s for s in self._iter_target_snapshots()
                 if self._read_manifest(s).get("status") == "completed"]
            c.sort(key=lambda p: p.name)
            return c

        def _rm_target(s: Path):
            for sub in s.iterdir():
                if sub.is_dir() and self.ops.is_subvolume(sub):
                    try:
                        self.ops.delete_subvolume(sub)
                    except CommandError:
                        pass
            shutil.rmtree(s, ignore_errors=True)
            pruned.append(s.name)
            self._emit("info", f"pruned target snapshot: {s.name}")

        # stale (partial/failed/running) older than newest completed
        completed = _completed()
        newest = completed[-1].name if completed else ""
        for s in self._iter_target_snapshots():
            st = self._read_manifest(s).get("status")
            if st in ("partial", "failed", "running") and s.name < newest:
                _rm_target(s)

        if self.cfg.max_snapshots > 0 and len(completed) > self.cfg.max_snapshots:
            for s in completed[:len(completed) - self.cfg.max_snapshots]:
                _rm_target(s)
            completed = _completed()

        try:
            pct = self.ops.disk_usage_percent(self.cfg.target_root)
            while pct > self.cfg.max_disk_percent and len(completed) > max(1, self.cfg.min_keep):
                _rm_target(completed[0])
                completed = _completed()
                pct = self.ops.disk_usage_percent(self.cfg.target_root)
            if pct > self.cfg.max_disk_percent:
                self._emit("warning",
                           f"drive at {pct:.0f}% with only {len(completed)} snapshot(s) "
                           f"left (MIN_KEEP={self.cfg.min_keep}) - not pruning further")
        except OSError:
            pass

        # -- local: keep the last LOCAL_KEEP per kind (they are only `-p`
        #    parents). Generous on purpose so a co-existing backup script's
        #    incremental chain is never broken. Never touch a recorded parent.
        keep_names = set(keep_local.values())
        by_kind: Dict[str, List[Path]] = {}
        d = self.cfg.local_snapshots_dir
        if d.is_dir():
            for entry in d.iterdir():
                if entry.is_dir() and _COMPACT_RE.match(entry.name):
                    by_kind.setdefault(_COMPACT_RE.match(entry.name).group(1), []).append(entry)
        keep_n = max(2, self.cfg.local_keep)
        for kind, entries in by_kind.items():
            entries.sort(key=lambda p: p.name)
            for old in entries[:-keep_n]:
                if old.name in keep_names:
                    continue
                try:
                    self.ops.delete_subvolume(old)
                    pruned.append(old.name)
                    self._emit("info", f"pruned local snapshot: {old.name}")
                except CommandError:
                    pass
        return pruned

    def _dry_run(self, name: str, target_is_btrfs: Optional[bool]) -> BackupResult:
        self._emit("stage", "DRY RUN - nothing will be written")
        prev = self.latest_completed_manifest() or {}
        self._emit("info", f"target: {self.cfg.target_root} "
                           f"({'btrfs send/receive' if target_is_btrfs else 'compressed .btrfs.zst streams'})")
        self._emit("info", f"would create snapshot: {name}")
        for mount in self.cfg.source_mounts:
            kind = "root" if mount == "/" else Path(mount).name
            p = prev.get("local_snapshots", {}).get(kind)
            self._emit("info", f"  {mount}: {'incremental from ' + p if p else 'full send'}")
        return BackupResult("completed", snapshot_name=name, message="dry run finished")
