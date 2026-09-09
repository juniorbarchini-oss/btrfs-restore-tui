"""
`restore-tui` - single entry point. With no arguments it shows a retro menu:

    [B]  Backup ahora
    [R]  Restaurar archivos / carpetas
    [F]  Recuperacion total (equipo recien instalado)
    [Q]  Salir

`restore-tui backup [...]` and `restore-tui restore` jump straight into a mode
(so do the `backup-now` / `restore-now` aliases). Privilege elevation is
deferred: the menu and status run unprivileged; picking Backup or Restore
re-execs that one command under sudo.
"""
import os
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .config import Config

_PKG_ROOT = Path(__file__).resolve().parent.parent
_PRESERVE = ("WAYLAND_DISPLAY,DISPLAY,XDG_RUNTIME_DIR,TERM,COLORTERM,"
             "SUDO_USER,USER,PYTHONPATH")
console = Console()


def _elevate_into(module_or_path: str, extra_args=None):
    """Re-exec a single mode under sudo, preserving the terminal env."""
    extra_args = extra_args or []
    os.environ["PYTHONPATH"] = f"{_PKG_ROOT}:{os.environ.get('PYTHONPATH', '')}".rstrip(":")
    if module_or_path.endswith(".py"):
        cmd = [sys.executable, module_or_path, *extra_args]
    else:
        cmd = [sys.executable, "-m", module_or_path, *extra_args]
    if os.geteuid() != 0:
        os.execvp("sudo", ["sudo", f"--preserve-env={_PRESERVE}", *cmd])
    os.execvp(cmd[0], cmd)


def _run_backup(argv):
    _elevate_into("btrfs_restore.cli_backup", argv)


def _run_restore():
    _elevate_into(str(_PKG_ROOT / "main.py"))


def _show_recovery_info():
    cfg = Config.load()
    loc = cfg.target_root or "(USB not mounted / TARGET_DIR unset)"
    body = Text()
    body.append("BARE-METAL RECOVERY\n\n", style="bold #00FF66")
    body.append("Each backup snapshot carries a self-contained ", style="#00FF66")
    body.append("restore.sh", style="bold yellow")
    body.append(" that reinstalls\npackages, AUR, flatpaks and restores the home "
                "tree - it needs only bash,\ncoreutils, rsync, pacman and "
                "btrfs-progs (no Python, no this app).\n\n", style="#00FF66")
    body.append("From a freshly installed Arch / live ISO:\n", style="#00FF66")
    body.append(f"  1.  mount the backup drive  ({loc})\n", style="dim white")
    body.append("  2.  cd <drive>/btrfs-restore/snapshots/<newest>\n", style="dim white")
    body.append("  3.  sudo ./restore.sh            # or: sudo ./restore.sh --root /mnt\n",
                style="dim white")
    body.append("\nTo recover just a few files instead, use ", style="#00FF66")
    body.append("Restore", style="bold yellow")
    body.append(" from the menu.", style="#00FF66")
    console.print(Panel(body, border_style="#00FF66", title="[bold #00FF66]Recuperacion total[/]"))


def _menu():
    cfg = Config.load()
    host = os.uname().nodename
    dest = cfg.target_root or cfg.remote_name if cfg.remote_host else "(sin destino)"
    header = Text()
    header.append("  BTRFS RESTORE TUI ", style="bold #00FF66")
    header.append("- AGY Time Machine\n", style="#00FF66")
    header.append(f"  Origen: {host}   FS: btrfs   Destino: {dest}\n\n", style="dim #00FF66")
    header.append("   [B]  Backup ahora\n", style="#00FF66")
    header.append("   [R]  Restaurar archivos / carpetas\n", style="#00FF66")
    header.append("   [F]  Recuperacion total (equipo recien instalado)\n", style="#00FF66")
    header.append("   [Q]  Salir", style="#00FF66")
    console.print(Panel(header, border_style="#00FF66"))

    while True:
        try:
            choice = console.input("[bold #00FF66]> [/]").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return 0
        if choice in ("b", "backup"):
            _run_backup([])
        elif choice in ("r", "restore", "restaurar"):
            _run_restore()
        elif choice in ("f", "full"):
            _show_recovery_info()
        elif choice in ("q", "quit", "salir", ""):
            return 0
        else:
            console.print("[yellow]  Opcion: B, R, F o Q[/]")


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] in ("backup", "backup-now"):
        _run_backup(args[1:])
    elif args and args[0] in ("restore", "restore-now"):
        _run_restore()
    elif args and args[0] in ("-h", "--help"):
        console.print(__doc__ or "")
    else:
        sys.exit(_menu())


if __name__ == "__main__":
    main()
