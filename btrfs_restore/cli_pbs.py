"""
`restore-tui pbs` / menu option [P] - push the newest local snapshot
(/.snapshots/root_<ts> and home_<ts>) to a Proxmox Backup Server datastore.

Separate from `backup-now`: taking the snapshot and sending it offsite are
two distinct, manually-triggered steps (no cron) - see config.conf for the
PBS_REPOSITORY / PBS_PASSWORD / PBS_FINGERPRINT settings this reads.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from rich.console import Console

from .config import Config

console = Console()


def _latest_snapshot(snapshots_dir: Path, kind: str) -> Optional[Path]:
    """Newest /.snapshots/{kind}_<timestamp> dir (name sorts = time sorts)."""
    if not snapshots_dir.is_dir():
        return None
    candidates = sorted(
        p for p in snapshots_dir.glob(f"{kind}_*")
        if p.is_dir() and not p.is_symlink()
    )
    return candidates[-1] if candidates else None


def run(argv=None) -> int:
    if os.geteuid() != 0:
        console.print("[red]This needs root (reading root-owned files under /.snapshots).[/]")
        return 1

    cfg = Config.load()

    if not cfg.pbs_repository or not cfg.pbs_password:
        console.print(
            "[yellow]No PBS target configured.[/] Add to "
            f"~/.config/btrfs-restore/config.conf (as user {cfg.user}):\n\n"
            "  PBS_REPOSITORY=user@realm!token@host:datastore\n"
            "  PBS_PASSWORD=<token secret>\n"
            "  PBS_FINGERPRINT=<server fingerprint, from 'proxmox-backup-manager cert info'>\n"
        )
        return 1

    if shutil.which("proxmox-backup-client") is None:
        console.print("[red]proxmox-backup-client not found - install it first "
                       "(e.g. `yay -S proxmox-backup-client-bin` on Arch).[/]")
        return 1

    root_snap = _latest_snapshot(cfg.local_snapshots_dir, "root")
    home_snap = _latest_snapshot(cfg.local_snapshots_dir, "home")
    if root_snap is None and home_snap is None:
        console.print(
            f"[yellow]No snapshots found under {cfg.local_snapshots_dir}.[/] "
            "Run Backup now [B] first to take one."
        )
        return 1

    archives = []
    if root_snap is not None:
        archives.append(f"root.pxar:{root_snap}")
        console.print(f"[#00FF66]root -> {root_snap.name}[/]")
    if home_snap is not None:
        archives.append(f"home.pxar:{home_snap}")
        console.print(f"[#00FF66]home -> {home_snap.name}[/]")

    cmd = [
        "proxmox-backup-client", "backup", *archives,
        "--repository", cfg.pbs_repository,
        "--backup-id", os.uname().nodename,
    ]

    env = dict(os.environ)
    env["PBS_PASSWORD"] = cfg.pbs_password
    if cfg.pbs_fingerprint:
        env["PBS_FINGERPRINT"] = cfg.pbs_fingerprint

    console.print(f"[dim #00FF66]-> {cfg.pbs_repository}[/]\n")
    try:
        proc = subprocess.run(cmd, env=env)
    except KeyboardInterrupt:
        return 130
    except FileNotFoundError:
        console.print("[red]proxmox-backup-client not found on PATH.[/]")
        return 1

    if proc.returncode == 0:
        console.print("\n[bold #00FF66]Backup to PBS complete.[/]")
    else:
        console.print(f"\n[red]proxmox-backup-client exited with code {proc.returncode}.[/]")
    return proc.returncode


def main() -> None:
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
