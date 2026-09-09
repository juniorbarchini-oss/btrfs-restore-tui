"""
`backup-now` entry point: read-only btrfs snapshots + `btrfs send | receive` to a
USB drive or SSH host, with a retro phosphor-green live dashboard.
"""
import argparse
import sys
import threading
import time
from collections import deque
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.theme import Theme

from .backup import BtrfsBackupEngine
from .config import Config, BACKUP_DIRNAME

console = Console(theme=Theme({
    "info": "green", "warning": "yellow", "error": "bold red",
    "success": "bold green", "progress": "bright_green", "stage": "bold bright_green",
}))


class Dashboard:
    def __init__(self, title: str):
        self.title = title
        self.stage = "Initializing backup engine..."
        self.progress = ""
        self.logs: deque = deque(maxlen=5)
        self.start = time.time()
        self.lock = threading.Lock()

    def on_event(self, event_type: str, message: str) -> None:
        with self.lock:
            if event_type == "stage":
                self.stage = message
            elif event_type == "progress":
                self.progress = message
            else:
                tag = {"warning": "[yellow]!", "error": "[red]x",
                       "success": "[green]+"}.get(event_type, " ")
                line = f"{tag} {message}"
                if line not in self.logs:
                    self.logs.append(line)

    def render(self) -> Panel:
        with self.lock:
            mins, secs = divmod(int(time.time() - self.start), 60)
            body = Text()
            body.append(f" [{mins:02d}:{secs:02d}] ", style="bold yellow")
            body.append("Stage:  ", style="dim green")
            body.append(f"{self.stage}\n", style="bold green")
            if self.progress:
                body.append("           ", style="dim green")
                body.append(f"{self.progress}\n", style="bright_green")
            body.append("\n Activity:\n", style="dim white")
            for line in (self.logs or ["  waiting..."]):
                body.append(f"  {line}\n", style="dim white")
            return Panel(body, title=f"[bold green]{self.title}[/bold green]",
                         border_style="green", padding=(1, 2))


def _header(cfg: Config, dry_run: bool) -> Panel:
    text = Text()
    text.append("Btrfs Restore TUI - INCREMENTAL BACKUP" +
                ("  (DRY RUN)\n" if dry_run else "\n"), style="bold green")
    text.append(f"Source:      {' '.join(cfg.source_mounts)}\n", style="white")

    targets = []
    if cfg.target_root:
        btrfs = cfg.target_is_btrfs()
        mode = {True: "subvolumes", False: ".btrfs.zst streams",
                None: "not mounted"}[btrfs]
        targets.append(f"USB {cfg.snapshots_dir}  ({mode})")
    if cfg.remote_host and cfg.remote_path:
        targets.append(f"SSH {cfg.remote_name} {cfg.remote_host}:{cfg.remote_path}")
    text.append("Targets:     " + ("\n             ".join(targets) or
                "(none - plug USB or set REMOTE_HOST)") + "\n", style="white")

    text.append(f"Config:      {cfg.config_source}\n", style="dim white")
    cap = f", max {cfg.max_snapshots}" if cfg.max_snapshots else ""
    text.append(f"Retention:   USB drive <= {cfg.max_disk_percent}%, keep >= "
                f"{cfg.min_keep}{cap};  local/SSH keep {cfg.local_keep}", style="dim white")
    return Panel(text, border_style="green")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="backup-now",
        description="Read-only btrfs snapshots + send/receive to USB or SSH.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be sent, write nothing")
    parser.add_argument("--max-disk-percent", type=int, metavar="PCT",
                        help="prune oldest snapshots once the drive passes this %% (default 80)")
    parser.add_argument("--keep", type=int, metavar="N",
                        help="hard cap on the number of completed snapshots kept")
    parser.add_argument("--min-keep", type=int, metavar="N",
                        help="never prune below this many snapshots (default 2)")
    parser.add_argument("--target", metavar="DIR",
                        help="override backup target mount point")
    parser.add_argument("--quiet", action="store_true",
                        help="plain line output instead of the live dashboard")
    args = parser.parse_args()

    cfg = Config.load()
    if args.max_disk_percent is not None:
        cfg.max_disk_percent = min(99, max(10, args.max_disk_percent))
    if args.keep is not None:
        cfg.max_snapshots = max(0, args.keep)
    if args.min_keep is not None:
        cfg.min_keep = max(1, args.min_keep)
    if args.target:
        root = Path(args.target)
        cfg.target_root = root if root.name == BACKUP_DIRNAME else root / BACKUP_DIRNAME

    console.print(_header(cfg, args.dry_run))

    if not cfg.target_root and not (cfg.remote_host and cfg.remote_path):
        console.print(Panel(
            "[bold red]No backup target found.[/bold red]\n"
            "Plug in the USB drive, set [bold]TARGET_DIR[/bold], or configure "
            "[bold]REMOTE_HOST[/bold] in [bold]~/.config/restore-tui/config.conf[/bold].",
            border_style="red"))
        sys.exit(2)

    if args.quiet or args.dry_run:
        def quiet_cb(t, m):
            if t == "progress":
                console.print(f"  {m}", style="progress", end="\r", highlight=False)
            else:
                console.print(f"[{t}] {m}", style=t)
        engine = BtrfsBackupEngine(cfg, callback=quiet_cb)
        result = engine.run(dry_run=args.dry_run)
    else:
        dash = Dashboard("Synchronizing snapshots")
        engine = BtrfsBackupEngine(cfg, callback=dash.on_event)
        stop = threading.Event()
        from rich.live import Live

        def refresh(live: "Live") -> None:
            while not stop.is_set():
                live.update(dash.render())
                time.sleep(0.12)

        with Live(dash.render(), console=console, refresh_per_second=8, transient=True) as live:
            rt = threading.Thread(target=refresh, args=(live,), daemon=True)
            rt.start()
            try:
                result = engine.run(dry_run=False)
            finally:
                stop.set()
                rt.join(timeout=1.0)

    _print_summary(result, args.dry_run)
    sys.exit(0 if result.ok else 1)


def _print_summary(result, dry_run: bool) -> None:
    if dry_run:
        style = "green" if result.ok else "red"
        console.print(Panel(
            f"[bold {style}]DRY RUN {'OK' if result.ok else 'FAILED'}[/bold {style}]\n"
            f"{result.message}", border_style=style))
        return

    parents = ", ".join(f"{k}:{v or 'full'}" for k, v in result.parents.items()) or "(full initial)"
    if result.status == "completed":
        extra = ""
        if result.warnings:
            extra = "\n[yellow]Warnings:[/yellow] " + "; ".join(result.warnings[:3])
        if result.pruned:
            extra += f"\n[dim]Pruned: {', '.join(result.pruned)}[/dim]"
        console.print(Panel(
            f"[bold green]BACKUP COMPLETED[/bold green]  ({result.duration_seconds}s)\n"
            f"Snapshot: {result.snapshot_name}\n"
            f"Parents:  {parents}\n"
            f"[dim]Run [bold]restore-now[/bold] to browse and recover files.[/dim]" + extra,
            border_style="bold green"))
    elif result.status == "partial":
        console.print(Panel(
            f"[bold yellow]BACKUP PARTIAL[/bold yellow]\n{result.message}\n"
            f"Snapshot kept for inspection: {result.snapshot_name}\n"
            "[dim]'latest' was NOT moved; next run will not use this as a parent.[/dim]",
            border_style="yellow"))
    else:
        console.print(Panel(
            f"[bold red]BACKUP FAILED[/bold red]\n{result.message}", border_style="red"))


if __name__ == "__main__":
    main()
