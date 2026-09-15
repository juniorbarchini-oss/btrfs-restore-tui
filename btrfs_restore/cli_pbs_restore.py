"""
`restore-tui pbs-restore` / menu option [X] - browse a snapshot already
stored on Proxmox Backup Server and restore files/folders from it.

Same [Space]-mark / [R]estore-to-original / [E]xtract-to-custom workflow as
the local/USB/remote browser in ui.py, reusing its engine and modals - only
the source differs: a read-only FUSE mount of the snapshot's root.pxar and
home.pxar (via `proxmox-backup-client mount`) instead of a btrfs subvolume.
"""
import getpass
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

from rich.console import Console
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.content import Content
from textual.containers import Container, Horizontal
from textual.widgets import Button, Footer, Header, Static, Tree
from textual.widgets.tree import TreeNode

from .config import Config
from .engine import RestoreEngine
from .models import ConflictResolution, RestoreItem
from .theme import RETRO_CSS
from .ui import ConfirmRestoreModal, ConfirmExitModal, CustomPathModal, ProgressModal

console = Console()


def _pbs_env(cfg: Config) -> dict:
    env = dict(os.environ)
    env["PBS_PASSWORD"] = cfg.pbs_password
    if cfg.pbs_fingerprint:
        env["PBS_FINGERPRINT"] = cfg.pbs_fingerprint
    return env


def _list_snapshots(cfg: Config, hostname: str) -> List[dict]:
    res = subprocess.run(
        ["proxmox-backup-client", "snapshot", "list", f"host/{hostname}",
         "--repository", cfg.pbs_repository, "--output-format", "json"],
        env=_pbs_env(cfg), capture_output=True, text=True,
    )
    if res.returncode != 0:
        console.print(f"[red]{res.stderr.strip() or 'could not list snapshots'}[/]")
        return []
    try:
        return json.loads(res.stdout)
    except ValueError:
        return []


def _pick_snapshot(snapshots: List[dict]) -> Optional[str]:
    """Interactive pick; returns the snapshot path e.g. host/dellomar/2026-...Z."""
    if not snapshots:
        console.print("[yellow]No snapshots found for this host on PBS.[/]")
        return None
    snapshots = sorted(snapshots, key=lambda s: s.get("backup-time", 0), reverse=True)
    console.print("\n[bold #00FF66]Snapshots on PBS (newest first):[/]\n")
    for i, s in enumerate(snapshots):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s["backup-time"]))
        gb = s.get("size", 0) / (1024 ** 3)
        mark = "  [dim](default)[/dim]" if i == 0 else ""
        console.print(f"  [{i}] {ts}  ({gb:.2f} GiB){mark}")
    choice = console.input("\n[bold #00FF66]Pick a snapshot number (Enter = newest): [/]").strip()
    idx = 0
    if choice:
        try:
            idx = int(choice)
        except ValueError:
            idx = 0
    if idx < 0 or idx >= len(snapshots):
        idx = 0
    s = snapshots[idx]
    ts_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(s["backup-time"]))
    return f"host/{s['backup-id']}/{ts_str}"


def _mount_archive(cfg: Config, snapshot: str, archive: str, target: Path) -> bool:
    target.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(
        ["proxmox-backup-client", "mount", snapshot, archive, str(target),
         "--repository", cfg.pbs_repository],
        env=_pbs_env(cfg), capture_output=True, text=True,
    )
    if res.returncode != 0:
        console.print(f"[red]mount {archive}: {res.stderr.strip()}[/]")
        return False
    # mount daemonizes; give the FUSE process a moment to attach.
    for _ in range(50):
        if any(target.iterdir()) if target.is_dir() else False:
            return True
        time.sleep(0.1)
    return target.is_dir()


def _unmount(target: Path) -> None:
    umount_bin = (shutil.which("fusermount3") or shutil.which("fusermount")
                  or shutil.which("umount"))
    if umount_bin:
        args = [umount_bin, "-uz", str(target)] if "fusermount" in umount_bin \
            else [umount_bin, str(target)]
        subprocess.run(args, capture_output=True)
    try:
        target.rmdir()
    except OSError:
        pass


class PBSRestoreApp(App):
    """Browse a FUSE-mounted PBS snapshot and restore files/folders from it."""

    CSS = RETRO_CSS
    TITLE = "Btrfs Restore TUI - PBS Restore"
    BINDINGS = [
        Binding("space", "toggle_select", "Select/Unselect", priority=True),
        Binding("r", "restore_original", "Restore Original"),
        Binding("e", "extract_custom", "Extract to..."),
        Binding("q", "quit_app", "Quit"),
    ]

    def __init__(self, snapshot_label: str, mounts: Dict[str, Dict]):
        """`mounts`: {"root": {"path": Path, "target_base": Path},
                       "home": {"path": Path, "target_base": Path}}"""
        super().__init__()
        self.snapshot_label = snapshot_label
        self.mounts = mounts
        self.engine = RestoreEngine()
        self.selected_paths: Set[Path] = set()
        self.node_path_map: Dict[str, TreeNode] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(f" PBS snapshot: {self.snapshot_label}", id="snapshot-bar")
        with Container(id="tree-container"):
            yield Tree("PBS Snapshot Explorer", id="file-tree")
        yield Static(id="status-box")
        with Horizontal(id="action-bar"):
            yield Button(Content("[ <R> Restore to Original ]"), id="btn-restore", variant="primary")
            yield Button(Content("[ <E> Extract to... ]"), id="btn-extract")
            yield Button(Content("[ <Q> Quit ]"), id="btn-quit")
        yield Footer()

    def on_mount(self) -> None:
        tree = self.query_one("#file-tree", Tree)
        tree.root.expand()
        for kind, info in self.mounts.items():
            label = Text(f"[{'SYSTEM (root.pxar)' if kind == 'root' else 'HOME (home.pxar)'}]",
                         style="bold #00FF66")
            branch = tree.root.add(
                label, data={"path": info["path"], "loaded": False, "is_dir": True,
                             "kind": kind, "target_base": info["target_base"]})
            branch.expand()
        self.update_status_bar()

    def _format_node_label(self, path: Path, is_dir: bool, is_selected: bool):
        size_str = ""
        if not is_dir and path.exists() and not path.is_symlink():
            try:
                sz = path.stat().st_size
                if sz < 1024:
                    size_str = f" ({sz} B)"
                elif sz < 1024 * 1024:
                    size_str = f" ({sz / 1024:.1f} KB)"
                else:
                    size_str = f" ({sz / (1024 * 1024):.1f} MB)"
            except OSError:
                pass
        mark = "[X] " if is_selected else "[ ] "
        icon = "\U0001F4C1 " if is_dir else "\U0001F4C4 "
        style = "bold yellow" if is_selected else "green"
        return Text(f"{mark}{icon}{path.name}{'/' if is_dir else ''}{size_str}", style=style)

    def _kind_for(self, node: TreeNode) -> Optional[str]:
        n = node
        while n is not None:
            if n.data and "kind" in n.data:
                return n.data["kind"]
            n = n.parent
        return None

    def _populate_node(self, node: TreeNode) -> None:
        if node.data.get("loaded"):
            return
        path: Path = node.data["path"]
        if not path.is_dir():
            node.data["loaded"] = True
            return
        try:
            entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            node.add_leaf(Text("permission denied", style="bold red"))
            node.data["loaded"] = True
            return
        for entry in entries:
            is_dir = entry.is_dir() and not entry.is_symlink()
            is_selected = entry in self.selected_paths
            label = self._format_node_label(entry, is_dir, is_selected)
            if is_dir:
                child = node.add(label, data={"path": entry, "loaded": False, "is_dir": True})
            else:
                child = node.add_leaf(label, data={"path": entry, "loaded": True, "is_dir": False})
            self.node_path_map[str(entry)] = child
        node.data["loaded"] = True

    def on_tree_node_expanded(self, event: Tree.NodeExpanded) -> None:
        self._populate_node(event.node)

    def action_toggle_select(self) -> None:
        tree = self.query_one("#file-tree", Tree)
        node = tree.cursor_node
        if not node or not node.data or "path" not in node.data:
            return
        path: Path = node.data["path"]
        is_dir = node.data.get("is_dir", False)
        if path in self.selected_paths:
            self.selected_paths.remove(path)
            is_selected = False
        else:
            self.selected_paths.add(path)
            is_selected = True
        node.label = self._format_node_label(path, is_dir, is_selected)
        self.update_status_bar()

    def update_status_bar(self) -> None:
        box = self.query_one("#status-box", Static)
        count = len(self.selected_paths)
        if count == 0:
            box.update(
                "No items selected. [Space] to mark/unmark, [Enter] to expand/collapse."
            )
            return
        total = 0
        for p in self.selected_paths:
            try:
                total += p.stat().st_size if p.is_file() else sum(
                    f.stat().st_size for f in p.rglob("*") if f.is_file())
            except OSError:
                pass
        gb = total / (1024 ** 3)
        box.update(f"{count} item(s) marked ({gb:.3f} GiB). "
                    "[R] Restore to Original  [E] Extract to...")

    def _grouped_items(self):
        """selected_paths split by which mount (root/home) they came from ->
        {kind: (items, target_base)}."""
        groups: Dict[str, List[Path]] = {}
        for p in self.selected_paths:
            for kind, info in self.mounts.items():
                try:
                    p.relative_to(info["path"])
                except ValueError:
                    continue
                groups.setdefault(kind, []).append(p)
                break
        out = {}
        for kind, paths in groups.items():
            root = self.mounts[kind]["path"]
            items = self.engine.prepare_items(paths, root)
            out[kind] = (items, self.mounts[kind]["target_base"])
        return out

    def action_restore_original(self) -> None:
        if not self.selected_paths:
            self.notify("Select at least one file or folder first (Space)", severity="warning")
            return
        groups = self._grouped_items()
        if len(groups) > 1:
            self.notify(
                "Marked items span SYSTEM and HOME - restore one group at a "
                "time (unmark the other, or use Extract to... for a mix).",
                severity="warning")
            return
        (items, target_base), = groups.values()
        system_restore = not self.engine._within_user_home(target_base)

        def on_confirm(resolution: Optional[ConflictResolution]):
            if resolution:
                self.run_restoration(items, target_base, resolution)

        self.push_screen(
            ConfirmRestoreModal(items, target_base, system_restore=system_restore),
            on_confirm)

    def action_extract_custom(self) -> None:
        if not self.selected_paths:
            self.notify("Select at least one file or folder first (Space)", severity="warning")
            return
        groups = self._grouped_items()
        all_items: List[RestoreItem] = []
        for items, _ in groups.values():
            all_items.extend(items)

        user = os.getenv("SUDO_USER") or os.getenv("USER") or getpass.getuser()
        default_dir = Path(f"/home/{user}") / f"pbs_recovered_{time.strftime('%Y%m%d_%H%M%S')}"

        def on_path(target_path: Optional[Path]):
            if target_path:
                def on_confirm(resolution: Optional[ConflictResolution]):
                    if resolution:
                        self.run_restoration(all_items, target_path, resolution)
                self.push_screen(ConfirmRestoreModal(all_items, target_path), on_confirm)

        self.push_screen(CustomPathModal(default_dir), on_path)

    @work(thread=True)
    def run_restoration(self, items: List[RestoreItem], target_base: Path,
                        resolution: ConflictResolution):
        progress_modal = ProgressModal(title="Restoring files...")
        self.app.call_from_thread(self.push_screen, progress_modal)
        gen = self.engine.restore_generator(items, target_base, resolution)
        for state in gen:
            self.app.call_from_thread(
                progress_modal.update_progress,
                state.spinner_idx, state.percent, state.current_file,
                state.done, state.error, state,
            )
            time.sleep(0.03)

    def action_quit_app(self) -> None:
        def on_exit_confirm(confirmed: Optional[bool]):
            if confirmed:
                self.exit()
        self.push_screen(ConfirmExitModal(), on_exit_confirm)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-restore":
            self.action_restore_original()
        elif event.button.id == "btn-extract":
            self.action_extract_custom()
        elif event.button.id == "btn-quit":
            self.action_quit_app()


def run(argv=None) -> int:
    if os.geteuid() != 0:
        console.print("[red]This needs root (restoring files to system paths).[/]")
        return 1

    cfg = Config.load()
    if not cfg.pbs_repository or not cfg.pbs_password:
        console.print(
            "[yellow]No PBS target configured - see config.conf "
            "(PBS_REPOSITORY / PBS_PASSWORD / PBS_FINGERPRINT).[/]"
        )
        return 1
    if shutil.which("proxmox-backup-client") is None:
        console.print("[red]proxmox-backup-client not found.[/]")
        return 1

    hostname = os.uname().nodename
    snapshots = _list_snapshots(cfg, hostname)
    snapshot = _pick_snapshot(snapshots)
    if not snapshot:
        return 1

    user = os.getenv("SUDO_USER") or os.getenv("USER") or getpass.getuser()
    run_dir = Path(tempfile.mkdtemp(prefix="pbs-restore-"))
    mounts = {
        "root": {"path": run_dir / "root", "target_base": Path("/")},
        "home": {"path": run_dir / "home", "target_base": Path(f"/home/{user}")},
    }

    console.print(f"[dim #00FF66]Mounting {snapshot} ...[/]")
    ok = True
    for kind, info in mounts.items():
        archive = f"{kind}.pxar"
        if not _mount_archive(cfg, snapshot, archive, info["path"]):
            ok = False

    if not ok:
        for info in mounts.values():
            _unmount(info["path"])
        shutil.rmtree(run_dir, ignore_errors=True)
        console.print("[red]Could not mount the snapshot - aborting.[/]")
        return 1

    try:
        app = PBSRestoreApp(snapshot, mounts)
        app.run()
    finally:
        for info in mounts.values():
            _unmount(info["path"])
        shutil.rmtree(run_dir, ignore_errors=True)
    return 0


def main() -> None:
    sys.exit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
