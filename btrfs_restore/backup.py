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
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .config import Config
from .btrfs_ops import BtrfsOps, CommandError, build_ssh_args
from .system_state import SystemStateCollector

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
    excluded: Dict[str, List[str]] = field(default_factory=dict)
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "completed"


class BtrfsBackupEngine:
    def __init__(self, cfg: Config, ops: Optional[BtrfsOps] = None,
                 callback: Optional[EventCallback] = None,
                 state_collector_cls=None):
        self.cfg = cfg
        self._external_cb = callback or (lambda t, m: None)
        self._logfile = None
        self._send_label = ""
        self._state_collector_cls = state_collector_cls or SystemStateCollector
        self.ops = ops or BtrfsOps(progress_cb=self._on_send_progress)

    def _on_send_progress(self, text: str) -> None:
        self._emit("progress", f"{self._send_label}{text}")

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

    # -- targets ----------------------------------------------------

    def _remote_enabled(self) -> bool:
        return bool(self.cfg.remote_host and self.cfg.remote_path)

    def _usb_enabled(self) -> bool:
        return bool(self.cfg.target_root) and self._target_writable()

    # -- main --------------------------------------------------------

    def run(self, dry_run: bool = False) -> BackupResult:
        name = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        start = time.time()
        usb = self._usb_enabled() if self.cfg.target_root else False
        remote = self._remote_enabled()

        if dry_run:
            return self._dry_run(name, usb, remote)

        if not usb and not remote:
            return BackupResult("failed", message=(
                "No backup target: plug in the USB drive, set TARGET_DIR, or "
                "configure REMOTE_HOST in ~/.config/restore-tui/config.conf."))
        if os.geteuid() != 0:
            return BackupResult("failed", message="backup needs root (btrfs snapshot/send)")

        result = BackupResult("failed", snapshot_name=name)
        compact = name.replace("-", "")
        state_dir: Optional[Path] = None
        local_snaps: Dict[str, str] = {}

        # log to the USB snapshot dir if there is one, else next to /.snapshots
        try:
            self.cfg.local_snapshots_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        logdir = self.cfg.local_snapshots_dir
        self._logfile = open(logdir / f".backup-{compact}.log", "w", encoding="utf-8")

        try:
            # 1. read-only local snapshots (shared by every target)
            for mount in self.cfg.source_mounts:
                kind = "root" if mount == "/" else Path(mount).name
                self._emit("stage", f"Snapshot {mount}")
                local = self.cfg.local_snapshots_dir / f"{kind}_{compact}"
                dropped = self._make_local_snapshot(
                    Path(mount), local, self.cfg.exclusions_for(mount))
                local_snaps[kind] = local.name
                if dropped:
                    result.excluded[kind] = dropped
                    self._emit("info", f"{kind}: left out {len(dropped)} cache/trash "
                                       f"path(s): {', '.join(dropped)}")

            # 2. system state (once)
            self._emit("stage", "System state (packages, config, restore.sh)")
            state_dir = (self.cfg.local_snapshots_dir / f".state-{compact}")
            state_dir.mkdir(parents=True, exist_ok=True)
            result.warnings += self._state_collector_cls(state_dir).collect_all()
            for w in result.warnings:
                self._emit("warning", w)

            outcomes = []
            if usb:
                outcomes.append(("USB", self._backup_to_usb(name, local_snaps, state_dir, result)))
            if remote:
                outcomes.append(("i7server", self._backup_to_remote(local_snaps, state_dir, result)))

            ok = [t for t, s in outcomes if s == "completed"]
            bad = [f"{t}: {s}" for t, s in outcomes if s != "completed"]
            result.duration_seconds = round(time.time() - start, 2)
            statuses = {s for _, s in outcomes}
            if statuses == {"completed"}:
                result.status = "completed"
                self._emit("success", f"Backup completed in {result.duration_seconds}s "
                                      f"-> {', '.join(ok)}")
            elif ok or statuses <= {"completed", "partial"}:
                # some target succeeded, or the only failure is a recoverable
                # (partial) btrfs error - keep it as partial, not a hard fail
                result.status = "partial"
                result.message = (f"ok: {', '.join(ok)}  |  " if ok else "") + \
                                 f"issues: {'; '.join(bad)}"
                self._emit("error", result.message)
            else:
                result.status = "failed"
                result.message = "; ".join(bad) or "all targets failed"
                self._emit("error", result.message)

            result.pruned += self._prune_local(local_snaps)

        except (CommandError, OSError, RuntimeError) as exc:
            result.status = "failed"
            result.message = str(exc)
            self._emit("error", result.message)
        finally:
            if state_dir and state_dir.exists():
                shutil.rmtree(state_dir, ignore_errors=True)
            if self._logfile:
                self._logfile.close()
                self._logfile = None
        return result

    def _backup_to_usb(self, name: str, local_snaps: Dict[str, str],
                       state_dir: Path, result: BackupResult) -> str:
        target_is_btrfs = self.cfg.target_is_btrfs()
        try:
            self.cfg.snapshots_dir.mkdir(parents=True, exist_ok=True)
            snap_dir = self._reserve_snapshot_dir(name)
        except OSError as exc:
            self._emit("error", f"USB: cannot create snapshot dir: {exc}")
            return "failed"

        result.snapshot_name = snap_dir.name
        result.snapshot_path = snap_dir
        prev = self.latest_completed_manifest() or {}
        prev_parents = prev.get("local_snapshots", {})
        self._write_manifest(snap_dir, status="running",
                             started_at=datetime.now().isoformat())
        try:
            # move the shared system state into this snapshot
            for item in state_dir.iterdir():
                shutil.move(str(item), str(snap_dir / item.name))

            parents = {}
            for kind, local_name in local_snaps.items():
                local = self.cfg.local_snapshots_dir / local_name
                parent = self._pick_parent(kind, prev_parents)
                parents[kind] = parent.name if parent else None
                self._emit("stage", f"USB: send {kind} "
                                    f"({'incremental' if parent else 'full'})")
                self._send_usb(kind, local, parent, snap_dir, target_is_btrfs)

            self._write_manifest(
                snap_dir, status="completed",
                created_at=datetime.now().isoformat(),
                target_is_btrfs=bool(target_is_btrfs),
                local_snapshots=local_snaps, parents=parents,
                excluded=result.excluded,
                warnings=result.warnings)
            self._update_latest(snap_dir)
            result.parents.update({f"usb/{k}": v for k, v in parents.items()})
            result.pruned += self._prune_usb()
            return "completed"
        except (CommandError, OSError, RuntimeError) as exc:
            self._emit("error", f"USB: {exc}")
            try:
                self._write_manifest(snap_dir, status="partial",
                                     failed_at=datetime.now().isoformat(), error=str(exc))
            except OSError:
                pass
            return "partial" if isinstance(exc, CommandError) else "failed"

    def _backup_to_remote(self, local_snaps: Dict[str, str], state_dir: Path,
                          result: BackupResult) -> str:
        user = self.cfg.user
        ssh = build_ssh_args(self.cfg, user)
        remote = f"{self.cfg.remote_user or user}@{self.cfg.remote_host}"
        base = self.cfg.remote_path.rstrip("/")
        try:
            mk = self.ops.ssh_capture(
                ssh, remote,
                ["mkdir", "-p", f"{base}/root", f"{base}/home", f"{base}/meta"])
            if mk.returncode != 0:
                self._emit("error", f"i7server: {mk.stderr.strip() or 'ssh failed'}")
                return "unreachable"

            for kind, local_name in local_snaps.items():
                local = self.cfg.local_snapshots_dir / local_name
                parent = self._remote_parent(kind, ssh, remote, base)
                self._emit("stage", f"i7server: send {kind} "
                                    f"({'incremental' if parent else 'full'})")
                self._send_label = f"i7server {kind} "
                rc = self.ops.send_ssh_receive(
                    local, parent, ssh, remote,
                    ["sudo", "btrfs", "receive", f"{base}/{kind}/"])
                if rc != 0:
                    raise CommandError(rc, f"send {kind} to i7server")
                result.parents[f"i7server/{kind}"] = parent.name if parent else None

            # system state -> <base>/meta/<ts>/
            self._emit("stage", "i7server: system state")
            self._push_state_remote(state_dir, ssh, remote, base)
            self._prune_remote(ssh, remote, base, result)
            return "completed"
        except CommandError as exc:
            self._emit("error", f"i7server: {exc}")
            return "partial"
        except (OSError, RuntimeError) as exc:
            self._emit("error", f"i7server: {exc}")
            return "failed"

    def _remote_parent(self, kind: str, ssh, remote: str, base: str) -> Optional[Path]:
        """Newest <kind>_* subvolume already on the remote that also exists in
        /.snapshots locally - the only valid `btrfs send -p` base."""
        try:
            res = self.ops.ssh_capture(
                ssh, remote, ["sudo", "btrfs", "subvolume", "list", "-o", f"{base}/{kind}"])
        except (OSError, subprocess.SubprocessError):
            return None
        names = sorted(
            line.split("path", 1)[1].strip().split("/")[-1]
            for line in res.stdout.splitlines() if "path" in line
        )
        names = [n for n in names if _COMPACT_RE.match(n) and n.startswith(f"{kind}_")]
        for n in reversed(names):
            local = self.cfg.local_snapshots_dir / n
            if local.is_dir():
                return local
        return None

    def _push_state_remote(self, state_dir: Path, ssh, remote: str, base: str) -> None:
        dest = f"{base}/meta/{state_dir.name.replace('.state-', '')}"
        try:
            rc, err = self.ops.push_tree(state_dir, ssh, remote, dest)
            if rc != 0:
                self._emit("warning", f"i7server: system state not pushed: {err or rc}")
        except (OSError, subprocess.SubprocessError) as exc:
            self._emit("warning", f"i7server: system state not pushed: {exc}")

    def _prune_remote(self, ssh, remote: str, base: str, result: BackupResult) -> None:
        keep = max(2, self.cfg.local_keep)
        for kind in ("root", "home"):
            try:
                res = self.ops.ssh_capture(
                    ssh, remote,
                    ["sudo", "btrfs", "subvolume", "list", "-o", f"{base}/{kind}"])
                names = sorted(
                    ln.split("path", 1)[1].strip().split("/")[-1]
                    for ln in res.stdout.splitlines() if "path" in ln)
                names = [n for n in names if n.startswith(f"{kind}_")]
                for old in names[:-keep]:
                    d = self.ops.ssh_capture(
                        ssh, remote,
                        ["sudo", "btrfs", "subvolume", "delete", f"{base}/{kind}/{old}"])
                    if d.returncode == 0:
                        result.pruned.append(f"i7server:{old}")
                        self._emit("info", f"i7server: pruned {old}")
            except (OSError, subprocess.SubprocessError) as exc:
                self._emit("warning", f"i7server: prune {kind} failed: {exc}")

    # -- steps -----------------------------------------------------

    def _make_local_snapshot(self, mount: Path, dest: Path,
                             exclusions: Optional[List[str]] = None) -> List[str]:
        """RO snapshot of `mount` at `dest`. When `exclusions` are given the
        snapshot is taken writable, the matching cache/trash paths are deleted,
        and it is flipped read-only before it is used as a `send` source or an
        incremental parent. Returns the paths actually removed."""
        if dest.exists():
            try:
                self.ops.delete_subvolume(dest)
            except CommandError:
                shutil.rmtree(dest, ignore_errors=True)
        if exclusions:
            self.ops.snapshot_rw(mount, dest)
            dropped = self.ops.prune_paths(dest, exclusions)
            self.ops.set_readonly(dest)
            return dropped
        self.ops.snapshot_ro(mount, dest)
        return []

    def _pick_parent(self, kind: str, prev_parents: Dict[str, str]) -> Optional[Path]:
        """The local snapshot recorded by the last completed backup for `kind`,
        if it still exists - that's the only safe `btrfs send -p` base."""
        name = prev_parents.get(kind)
        if not name:
            return None
        p = self.cfg.local_snapshots_dir / name
        return p if p.is_dir() else None

    def _send_usb(self, kind: str, local: Path, parent: Optional[Path],
                  snap_dir: Path, target_is_btrfs: Optional[bool]) -> None:
        self._send_label = f"USB {kind} "
        if target_is_btrfs:
            rc = self.ops.send_local_receive(local, parent, snap_dir)
        else:
            rc = self.ops.send_to_stream(local, parent, snap_dir / f"{kind}.btrfs.zst")
        if rc != 0:
            raise CommandError(rc, f"btrfs send {kind}")

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

    def _prune_usb(self) -> List[str]:
        pruned: List[str] = []
        if not self.cfg.snapshots_dir:
            return pruned

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
        return pruned

    def _prune_local(self, keep_local: Dict[str, str]) -> List[str]:
        # keep the last LOCAL_KEEP per kind (they are only `-p` parents).
        # Generous on purpose so a co-existing backup script's incremental chain
        # is never broken. Never touch a recorded parent.
        pruned: List[str] = []
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

    def _dry_run(self, name: str, usb: bool, remote: bool) -> BackupResult:
        self._emit("stage", "DRY RUN - nothing will be written")
        self._emit("info", f"would create snapshot: {name}")
        targets = []
        if usb:
            tb = self.cfg.target_is_btrfs()
            targets.append(f"USB {self.cfg.target_root} "
                           f"({'btrfs send/receive' if tb else '.btrfs.zst streams'})")
        if remote:
            targets.append(f"i7server {self.cfg.remote_host}:{self.cfg.remote_path}")
        if not targets:
            return BackupResult("failed", snapshot_name=name,
                                message="no target (plug USB or set REMOTE_HOST)")
        for t in targets:
            self._emit("info", f"target: {t}")
        self._emit("info", f"sources: {' '.join(self.cfg.source_mounts)} "
                           "(incremental per target when a shared parent exists)")
        for mount in self.cfg.source_mounts:
            ex = self.cfg.exclusions_for(mount)
            if ex:
                self._emit("info", f"{mount}: would leave out {len(ex)} cache/trash glob(s)")
        return BackupResult("completed", snapshot_name=name, message="dry run finished")
