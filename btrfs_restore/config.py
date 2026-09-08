"""
Configuration for Btrfs Restore TUI.

Resolution order for every setting:
  1. Environment variable   (RESTORE_TUI_*  — legacy BTRFS_* still honoured)
  2. Config file            (~/.config/restore-tui/config.conf, then
                             ~/.config/btrfs-restore/config.conf, then /etc/*)
  3. Built-in default

The config file is NEVER rewritten or deleted by the program. `config.conf.example`
in the repo documents every key.

Schema is kept deliberately identical to the ext4 sibling `restore-tui` so the two
editions can later merge into one app with a selectable backend.
"""
import logging
import os
import getpass
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("btrfs_restore")

# Folder created on the backup drive / remote host to hold this tool's snapshots.
BACKUP_DIRNAME = "btrfs-restore"

# Snapshot directory names are "YYYY-MM-DD_HHMMSS" -> 17 chars (shared with ext4).
SNAPSHOT_NAME_LEN = 17

# Retention defaults (shared with ext4): after a successful backup, prune the
# oldest completed snapshots until the target drive is back under
# MAX_DISK_PERCENT, never below MIN_KEEP, never the last. MAX_SNAPSHOTS (0 = off)
# is an extra hard cap on the count.
DEFAULT_MAX_DISK_PERCENT = 80
DEFAULT_MIN_KEEP = 2
DEFAULT_MAX_SNAPSHOTS = 0

# How many local RO snapshots to keep per kind (root/home). They only serve as
# `btrfs send -p` parents for the next run; kept generous so a co-existing
# backup script's incremental chain is never broken.
DEFAULT_LOCAL_KEEP = 10

# Subvolumes captured by `backup-now`. Mount points, not subvol names.
DEFAULT_SOURCE_MOUNTS = ["/", "/home"]

# Where read-only local snapshots are created / looked for.
DEFAULT_LOCAL_SNAPSHOTS_DIR = Path("/.snapshots")

_ENV_PREFIX = "RESTORE_TUI_"

# Legacy config-file keys -> canonical key. Honoured with a deprecation warning.
_LEGACY_FILE_KEYS = {
    "BTRFS_REMOTE_HOST": "REMOTE_HOST",
    "BTRFS_REMOTE_PATH": "REMOTE_PATH",
    "BTRFS_REMOTE_USER": "REMOTE_USER",
    "BTRFS_REMOTE_NAME": "REMOTE_NAME",
    "BTRFS_REMOTE_PORT": "REMOTE_PORT",
}


def _current_user() -> str:
    return os.getenv("SUDO_USER") or os.getenv("USER") or getpass.getuser()


def _home_of(user: str) -> Path:
    """Real home of `user`, even when running under sudo."""
    try:
        import pwd

        return Path(pwd.getpwnam(user).pw_dir)
    except (KeyError, ImportError):
        return Path(f"/home/{user}")


def _config_candidates(user: str) -> List[Path]:
    home = _home_of(user)
    return [
        home / ".config" / "restore-tui" / "config.conf",
        home / ".config" / "btrfs-restore" / "config.conf",
        Path("/etc/restore-tui/config.conf"),
        Path("/etc/btrfs-restore/config.conf"),
    ]


def _read_config_file(user: str) -> dict:
    """First existing candidate wins. Returns {} if none found or unreadable."""
    for path in _config_candidates(user):
        try:
            if not path.is_file():
                continue
        except OSError:
            continue

        values: dict = {}
        excludes: List[str] = []
        legacy_seen: List[str] = []
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip().upper()
                val = val.strip().strip('"').strip("'")
                if key in _LEGACY_FILE_KEYS:
                    legacy_seen.append(key)
                    key = _LEGACY_FILE_KEYS[key]
                if key == "EXCLUDE":
                    if val:
                        excludes.append(val)
                else:
                    values[key] = val
        except OSError:
            return {}

        if legacy_seen:
            canon = sorted({_LEGACY_FILE_KEYS[k] for k in legacy_seen})
            logger.warning(
                "config %s uses deprecated keys %s - rename to %s",
                path, ", ".join(sorted(set(legacy_seen))), ", ".join(canon),
            )
        if excludes:
            values["_EXCLUDES"] = excludes
        values["_SOURCE"] = str(path)
        return values
    return {}


def _env(name: str) -> Optional[str]:
    """RESTORE_TUI_<NAME>, falling back to the legacy BTRFS_<NAME> spelling."""
    v = os.getenv(_ENV_PREFIX + name)
    if v not in (None, ""):
        return v
    legacy = os.getenv("BTRFS_" + name)
    if legacy in (None, "") and name.startswith("REMOTE_"):
        legacy = os.getenv("BTRFS_REMOTE_" + name[len("REMOTE_"):])
    if legacy not in (None, ""):
        logger.warning("env BTRFS_%s is deprecated - use %s%s", name, _ENV_PREFIX, name)
    return legacy


@dataclass
class Config:
    user: str = field(default_factory=_current_user)

    # -- backup source (btrfs) ------------------------------------------
    source_mounts: List[str] = field(default_factory=lambda: list(DEFAULT_SOURCE_MOUNTS))
    local_snapshots_dir: Path = DEFAULT_LOCAL_SNAPSHOTS_DIR
    staging_dir: Optional[Path] = None            # resolved; None -> engine detects

    # -- backup target (USB / dir) ------------------------------------
    target_root: Optional[Path] = None           # <mount>/btrfs-restore  (resolved)

    # -- retention (shared schema with ext4) --------------------------
    max_disk_percent: int = DEFAULT_MAX_DISK_PERCENT
    min_keep: int = DEFAULT_MIN_KEEP
    max_snapshots: int = DEFAULT_MAX_SNAPSHOTS
    local_keep: int = DEFAULT_LOCAL_KEEP

    # -- remote (SSH) target ----------------------------------------
    remote_host: str = ""
    remote_path: str = ""
    remote_user: str = ""
    remote_port: int = 22
    remote_name: str = "Remote"

    config_source: str = "(defaults)"

    # -- derived paths (same names/shape as ext4) --------------------
    @property
    def snapshots_dir(self) -> Optional[Path]:
        return self.target_root / "snapshots" if self.target_root else None

    @property
    def latest_link(self) -> Optional[Path]:
        return self.target_root / "latest" if self.target_root else None

    # -- loader --------------------------------------------------------
    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        fv = _read_config_file(cfg.user)

        if "_SOURCE" in fv:
            cfg.config_source = fv["_SOURCE"]

        # --- btrfs-specific dirs -------------------------------------
        staging = _env("STAGING_DIR") or fv.get("STAGING_DIR")
        if staging:
            cfg.staging_dir = Path(staging)

        snaps = _env("LOCAL_SNAPSHOTS_DIR") or fv.get("LOCAL_SNAPSHOTS_DIR")
        if snaps:
            cfg.local_snapshots_dir = Path(snaps)

        # --- target directory ----------------------------------------
        target_dir = (
            _env("TARGET")
            or _env("TARGET_DIR")
            or fv.get("TARGET_DIR")
            or _autodetect_backup_target(cfg.user)
        )
        if target_dir:
            root = Path(target_dir)
            if root.name != BACKUP_DIRNAME:
                root = root / BACKUP_DIRNAME
            cfg.target_root = root

        # --- retention ---------------------------------------------
        def _int(*vals, lo=0):
            for v in vals:
                if v not in (None, ""):
                    try:
                        return max(lo, int(v))
                    except (TypeError, ValueError):
                        pass
            return None

        pct = _int(_env("MAX_DISK_PERCENT"), fv.get("MAX_DISK_PERCENT"), lo=10)
        if pct is not None:
            cfg.max_disk_percent = min(99, pct)

        mk = _int(_env("MIN_KEEP"), fv.get("MIN_KEEP"), lo=1)
        if mk is not None:
            cfg.min_keep = mk

        lk = _int(_env("LOCAL_KEEP"), fv.get("LOCAL_KEEP"), lo=1)
        if lk is not None:
            cfg.local_keep = lk

        cap = _int(_env("KEEP"), _env("MAX_SNAPSHOTS"),
                   fv.get("MAX_SNAPSHOTS"), fv.get("KEEP_SNAPSHOTS"))
        if cap is not None:
            cfg.max_snapshots = cap

        # --- remote --------------------------------------------------
        cfg.remote_host = _env("REMOTE_HOST") or fv.get("REMOTE_HOST", "")
        cfg.remote_path = _env("REMOTE_PATH") or fv.get("REMOTE_PATH", "")
        cfg.remote_user = _env("REMOTE_USER") or fv.get("REMOTE_USER", "") or cfg.user
        cfg.remote_name = _env("REMOTE_NAME") or fv.get("REMOTE_NAME", "") or "Remote"
        try:
            cfg.remote_port = int(_env("REMOTE_PORT") or fv.get("REMOTE_PORT", "22"))
        except (TypeError, ValueError):
            cfg.remote_port = 22
        if cfg.remote_name == "Remote" and cfg.remote_host:
            cfg.remote_name = cfg.remote_host

        return cfg

    # -- helpers ------------------------------------------------------
    def target_is_btrfs(self) -> Optional[bool]:
        """True/False if the target FS type could be determined, else None."""
        if not self.target_root:
            return None
        fstype = _fstype_of(self.target_root)
        return None if fstype is None else fstype == "btrfs"


def _fstype_of(path: Path) -> Optional[str]:
    """Filesystem type of the mount that contains `path`, via findmnt."""
    if not shutil.which("findmnt"):
        return None
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        res = subprocess.run(
            ["findmnt", "-n", "-o", "FSTYPE", "-T", str(probe)],
            capture_output=True, text=True, timeout=5,
        )
        return res.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _autodetect_backup_target(user: str) -> Optional[str]:
    """Pick a removable-media mount to back up to.

    Preference: a drive that already holds a btrfs-restore backup > a drive
    labelled USB_BTRFS (the btrfs half of Humberto's USB) > the first writable
    removable mount found.
    """
    bases = [Path(f"/run/media/{user}"), Path(f"/media/{user}"), Path("/mnt")]
    candidates: List[Path] = []
    for base in bases:
        try:
            if not base.is_dir():
                continue
            for entry in sorted(base.iterdir()):
                if entry.is_dir() and os.access(entry, os.W_OK):
                    candidates.append(entry)
        except OSError:
            continue

    if not candidates:
        return None

    for drive in candidates:
        if (drive / BACKUP_DIRNAME / "snapshots").is_dir():
            return str(drive)
    for drive in candidates:
        if drive.name.upper() in ("USB_BTRFS", "USB_DATA"):
            return str(drive)
    return str(candidates[0])


def _dump() -> None:
    """`python -m btrfs_restore.config` - show the effective configuration."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    cfg = Config.load()
    btrfs = cfg.target_is_btrfs()
    rows = [
        ("config file", cfg.config_source),
        ("user", cfg.user),
        ("source mounts", " ".join(cfg.source_mounts)),
        ("local snapshots dir", str(cfg.local_snapshots_dir)),
        ("staging dir", str(cfg.staging_dir) if cfg.staging_dir else "(auto-detect)"),
        ("target root", str(cfg.target_root) if cfg.target_root else "(none - plug in USB or set TARGET_DIR)"),
        ("target is btrfs", {True: "yes", False: "no (streams .btrfs.zst)", None: "unknown"}[btrfs]),
        ("snapshots dir", str(cfg.snapshots_dir) if cfg.snapshots_dir else "-"),
        ("latest link", str(cfg.latest_link) if cfg.latest_link else "-"),
        ("retention", f"max_disk={cfg.max_disk_percent}%  min_keep={cfg.min_keep}  "
                      f"max_snapshots={cfg.max_snapshots or 'off'}  local_keep={cfg.local_keep}"),
        ("remote", f"{cfg.remote_name}  {cfg.remote_user}@{cfg.remote_host or '(none)'}:{cfg.remote_path or ''}  port {cfg.remote_port}"),
    ]
    width = max(len(k) for k, _ in rows)
    print("\nEffective Btrfs Restore TUI configuration")
    print("=" * (width + 40))
    for key, val in rows:
        print(f"  {key.ljust(width)} : {val}")
    print()


if __name__ == "__main__":
    _dump()
