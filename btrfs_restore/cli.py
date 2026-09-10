"""
`restore-tui` - single entry point. With no arguments it shows a retro menu:

    [B]  Backup now
    [R]  Restore files / folders
    [F]  Full recovery (freshly installed machine)
    [Q]  Quit

`restore-tui backup [...]` and `restore-tui restore` jump straight into a mode
(so do the `backup-now` / `restore-now` aliases). Privilege elevation is
deferred: the menu and status run unprivileged; picking Backup or Restore
re-execs that one command under sudo.
"""
import os
import subprocess
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


def _run_mode(module_or_path: str, extra_args=None) -> int:
    """Run one mode under sudo as a child process and return its exit code, so
    control comes back to the menu afterwards. The child inherits the terminal
    (stdin/stdout/stderr), so the TUI and the sudo prompt both work."""
    extra_args = extra_args or []
    os.environ["PYTHONPATH"] = f"{_PKG_ROOT}:{os.environ.get('PYTHONPATH', '')}".rstrip(":")
    if module_or_path.endswith(".py"):
        cmd = [sys.executable, module_or_path, *extra_args]
    else:
        cmd = [sys.executable, "-m", module_or_path, *extra_args]
    if os.geteuid() != 0:
        cmd = ["sudo", f"--preserve-env={_PRESERVE}", *cmd]
    try:
        return subprocess.run(cmd).returncode
    except KeyboardInterrupt:
        return 130


def _run_backup(argv=None) -> int:
    return _run_mode("btrfs_restore.cli_backup", argv or [])


def _run_restore() -> int:
    return _run_mode(str(_PKG_ROOT / "main.py"))


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
    console.print(Panel(body, border_style="#00FF66", title="[bold #00FF66]Full recovery[/]"))


def _print_menu():
    cfg = Config.load()
    host = os.uname().nodename
    dest = (cfg.target_root or (cfg.remote_name if cfg.remote_host else None)
            or "(no target - plug in the USB)")
    header = Text()
    header.append("  BTRFS RESTORE TUI ", style="bold #00FF66")
    header.append("- AGY Time Machine\n", style="#00FF66")
    header.append(f"  Source: {host}   FS: btrfs   Target: {dest}\n\n", style="dim #00FF66")
    header.append("   [B]  Backup now\n", style="#00FF66")
    header.append("   [R]  Restore files / folders\n", style="#00FF66")
    header.append("   [F]  Full recovery (freshly installed machine)\n", style="#00FF66")
    header.append("   [Q]  Quit", style="#00FF66")
    console.print(Panel(header, border_style="#00FF66"))


def _paths() -> int:
    """`restore-tui --paths` - every location the tool creates or touches."""
    cfg = Config.load()
    home = Path(f"/home/{os.getenv('SUDO_USER') or os.getenv('USER') or 'you'}")
    rows = [
        ("/opt/btrfs-restore-tui/", "installed app + venv (install.sh)"),
        ("/usr/local/bin/{restore-tui,restore-now,backup-now}", "command symlinks (install.sh)"),
        ("/usr/share/applications/btrfs-restore.desktop", "desktop entry (install.sh)"),
        (f"{home}/.config/btrfs-restore/config.conf", "user config - never auto-written"),
        ("/.snapshots/{root,home}_<ts>", "local read-only snapshots (btrfs send -p parents)"),
        ("/.snapshots/.backup-tmp/<pid>-<ts>/", "transient backup state - swept on start / atexit"),
        ("/.snapshots/.backup-last.log", "last backup log (single file, overwritten)"),
        ("/.snapshots/staging/<pid>-<slug>/", "restore staging subvolume - swept on start / atexit"),
        ("/.snapshots/.remote-mnt/<pid>-<slug>/", "read-only SSHFS mount of a remote snapshot - unmounted on start / atexit"),
        (str(cfg.target_root or "<USB>/btrfs-restore/"), "backups on the USB / target drive"),
        (f"{cfg.remote_host or '<remote>'}:{cfg.remote_path or '<path>'}/{{root,home,meta}}/",
         "backups on the SSH host"),
    ]
    w = max(len(p) for p, _ in rows)
    console.print("\n[bold #00FF66]Paths this tool creates or modifies[/]\n")
    for p, note in rows:
        console.print(f"  [#00FF66]{p.ljust(w)}[/]  [dim white]{note}[/dim white]")
    console.print()
    return 0


def _gc() -> int:
    """`restore-tui --gc` - drop staging + backup scratch left by dead runs."""
    import shutil as _sh
    from .engine import RestoreEngine
    from .backup import BtrfsBackupEngine

    if os.geteuid() != 0:
        os.environ["PYTHONPATH"] = f"{_PKG_ROOT}:{os.environ.get('PYTHONPATH', '')}".rstrip(":")
        return subprocess.run(
            ["sudo", f"--preserve-env={_PRESERVE}", sys.executable, "-m", "btrfs_restore.cli", "--gc"]
        ).returncode

    probe = Path("/.snapshots")
    before = _sh.disk_usage(probe).free if probe.exists() else 0
    RestoreEngine().cleanup_staging(orphans_only=True)
    BtrfsBackupEngine(Config.load())._sweep_stale_tmp()
    after = _sh.disk_usage(probe).free if probe.exists() else 0

    freed = after - before
    if freed > 1 << 20:
        console.print(f"[#00FF66]freed ~{freed / (1 << 30):.2f} GiB[/]")
    else:
        console.print("[#00FF66]nothing to collect - no dead-run leftovers[/]")
    return 0


def _menu() -> int:
    _print_menu()
    while True:
        try:
            choice = console.input("[bold #00FF66]> [/]").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return 0
        if choice in ("b", "backup"):
            _run_backup()
            console.print()
            _print_menu()
        elif choice in ("r", "restore", "restaurar"):
            _run_restore()
            console.print()
            _print_menu()
        elif choice in ("f", "full"):
            _show_recovery_info()
        elif choice in ("q", "quit", "salir", ""):
            return 0
        else:
            console.print("[yellow]  Pick one: B, R, F or Q[/]")


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] in ("backup", "backup-now"):
        sys.exit(_run_backup(args[1:]))
    elif args and args[0] in ("restore", "restore-now"):
        sys.exit(_run_restore())
    elif args and args[0] == "--gc":
        sys.exit(_gc())
    elif args and args[0] == "--paths":
        sys.exit(_paths())
    elif args and args[0] in ("-h", "--help"):
        console.print(__doc__ or "")
        console.print("  restore-tui --gc      remove staging / scratch from dead runs")
        console.print("  restore-tui --paths   list every path the tool touches")
    else:
        sys.exit(_menu())


if __name__ == "__main__":
    main()
