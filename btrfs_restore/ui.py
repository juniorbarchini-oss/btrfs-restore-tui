"""
Terminal User Interface (TUI) for Btrfs Restore TUI using Textual.
Retro phosphor-green aesthetic with bright amber highlight for selections.
"""
import os
import atexit
import signal
import getpass
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.content import Content
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, ProgressBar, Static, Tree
from textual.widgets.tree import TreeNode

from .engine import ConflictResolution, RestoreEngine, RestoreItem
from .models import SnapshotInfo, SnapshotType
from .scanner import SnapshotScanner
from .theme import RETRO_CSS, SPINNER_FRAMES


def _unwind_on_signal(signum, _frame):
    """Signal-safe: just raise. atexit + on_unmount do the staging cleanup;
    calling subprocess or App.exit() from signal context can hang."""
    raise SystemExit(128 + signum)


class SnapshotSelectModal(ModalScreen[Optional[SnapshotInfo]]):
    """Retro modal to select which Btrfs snapshot to explore."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, snapshots: List[SnapshotInfo], current_id: Optional[str] = None):
        super().__init__()
        self.snapshots = snapshots
        self.current_id = current_id
        self.button_snap_map: Dict[str, SnapshotInfo] = {}

    def action_cancel(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-dialog"):
            yield Label("▼ SELECT BTRFS SOURCE SNAPSHOT ▼", id="modal-title")
            with VerticalScroll(id="modal-scroll-content"):
                for idx, snap in enumerate(self.snapshots):
                    btn_id = f"snap_idx_{idx}"
                    self.button_snap_map[btn_id] = snap
                    prefix = "► " if snap.id == self.current_id else "  "
                    if snap.snap_type == SnapshotType.LOCAL:
                        icon = "📁"
                    elif snap.snap_type == SnapshotType.REMOTE:
                        icon = "☁️"
                    else:
                        icon = "💾"
                    label_text = f"{prefix}[{snap.snap_type.value}] {icon} {snap.name}"
                    yield Button(Content(label_text), id=btn_id, classes="snap-btn")
            with Horizontal(id="modal-buttons"):
                yield Button("<C> Cancel (Esc)", id="btn-cancel", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "btn-cancel":
            self.dismiss(None)
        elif button_id and button_id in self.button_snap_map:
            self.dismiss(self.button_snap_map[button_id])


class ConfirmRestoreModal(ModalScreen[Optional[ConflictResolution]]):
    """Confirmation modal prior to file restoration."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("b", "choose_bak", "Backup (.bak)"),
        Binding("o", "choose_overwrite", "Overwrite"),
        Binding("s", "choose_skip", "Skip existing"),
    ]

    def __init__(self, items: List[RestoreItem], target_path: Path,
                 system_restore: bool = False):
        super().__init__()
        self.items = items
        self.target_path = target_path
        self.system_restore = system_restore

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_choose_bak(self) -> None:
        self.dismiss(ConflictResolution.BACKUP)

    def action_choose_overwrite(self) -> None:
        self.dismiss(ConflictResolution.OVERWRITE)

    def action_choose_skip(self) -> None:
        self.dismiss(ConflictResolution.SKIP)

    def compose(self) -> ComposeResult:
        total_size = sum(i.size_bytes for i in self.items)
        if total_size < 1024 * 1024:
            size_str = f"{total_size / 1024:.1f} KB"
        else:
            size_str = f"{total_size / (1024 * 1024):.2f} MB"

        with Vertical(id="modal-dialog"):
            yield Label("⚠️  CONFIRM RESTORATION  ⚠️", id="modal-title")
            with Vertical(id="modal-content"):
                yield Label(f"Target: [bold yellow]{self.target_path}[/bold yellow]")
                yield Label(f"Items to restore: [bold cyan]{len(self.items)}[/bold cyan] ({size_str})")
                if self.system_restore:
                    yield Label(
                        "\n[bold red]SYSTEM restore[/bold red] - files keep their "
                        "original owner (root). You may need to reload services "
                        "or reboot afterwards.")
                yield Label("\nIf files with matching names already exist:")
            with Horizontal(id="modal-buttons"):
                yield Button("<B> Backup (.bak)", id="btn-bak", variant="primary")
                yield Button("<O> Overwrite", id="btn-overwrite", variant="warning")
                yield Button("<S> Skip existing", id="btn-skip", variant="default")
                yield Button("<C> Cancel (Esc)", id="btn-cancel", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-bak":
            self.dismiss(ConflictResolution.BACKUP)
        elif event.button.id == "btn-overwrite":
            self.dismiss(ConflictResolution.OVERWRITE)
        elif event.button.id == "btn-skip":
            self.dismiss(ConflictResolution.SKIP)
        else:
            self.dismiss(None)


class CustomPathModal(ModalScreen[Optional[Path]]):
    """Modal to specify a custom extraction target directory."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, default_path: Path):
        super().__init__()
        self.default_path = default_path

    def action_cancel(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-dialog"):
            yield Label("📁 EXTRACT TO CUSTOM DIRECTORY", id="modal-title")
            with Vertical(id="modal-content"):
                yield Label("Enter absolute or relative destination path:")
                yield Input(value=str(self.default_path), id="input-path")
            with Horizontal(id="modal-buttons"):
                yield Button("<E> Confirm", id="btn-ok", variant="primary")
                yield Button("<C> Cancel (Esc)", id="btn-cancel", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-ok":
            input_val = self.query_one(Input).value.strip()
            path = Path(input_val).expanduser().resolve()
            self.dismiss(path)
        else:
            self.dismiss(None)


class ProgressModal(ModalScreen[None]):
    """Retro modal with active ASCII spinner and progress bar."""

    BINDINGS = [
        Binding("escape", "handle_key_dismiss", "Close / Cancel"),
        Binding("c", "handle_key_dismiss", "Close / Cancel"),
        Binding("enter", "handle_key_dismiss", "Close"),
    ]

    def __init__(
        self,
        title: str = "Restoring files...",
        can_cancel: bool = False,
        on_cancel: Optional[Callable[[], None]] = None,
    ):
        super().__init__()
        self.title_text = title
        self.can_cancel = can_cancel
        self.on_cancel = on_cancel
        self.is_done = False

    def action_handle_key_dismiss(self) -> None:
        if self.is_done:
            self.dismiss(None)
        elif self.can_cancel:
            self.trigger_cancel()

    def trigger_cancel(self) -> None:
        if self.on_cancel:
            self.on_cancel()
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        with Vertical(id="progress-box"):
            yield Label(f"[ | ] {self.title_text}", id="progress-spinner")
            yield ProgressBar(total=100, show_eta=False, id="progress-bar")
            yield Label("", id="progress-filename")
            with Horizontal(id="modal-buttons"):
                if self.can_cancel:
                    yield Button(Content("[ <C> Cancel ]"), id="btn-cancel")
                btn = Button(Content("[ <Enter> Close ]"), id="btn-done")
                btn.display = False
                yield btn

    def update_progress(
        self,
        spinner_idx: int,
        percent: int,
        current_file: str,
        done: bool = False,
        error: Optional[str] = None,
        state=None,
    ):
        # Once a terminal state (done / error) has been shown, ignore any
        # trailing non-terminal updates so an error message can't be overwritten
        # by a later spinner frame.
        if self.is_done and not (done or error):
            return

        spinner_lbl = self.query_one("#progress-spinner", Label)
        p_bar = self.query_one("#progress-bar", ProgressBar)
        file_lbl = self.query_one("#progress-filename", Label)
        btn_done = self.query_one("#btn-done", Button)

        def _finish():
            if self.can_cancel:
                try:
                    self.query_one("#btn-cancel", Button).display = False
                except Exception:
                    pass
            btn_done.display = True
            btn_done.focus()

        restored = getattr(state, "processed_files", None)
        total = getattr(state, "total_files", None)
        failed = getattr(state, "failed_files", 0)
        fatal = getattr(state, "fatal", False)

        if error and done and not fatal:
            # some files went through, some failed - partial success
            self.is_done = True
            head = "[bold yellow]⚠  Restored with errors[/bold yellow]"
            if restored is not None and total is not None:
                head += f"  [dim]({restored}/{total} ok, {failed} failed)[/dim]"
            spinner_lbl.update(head)
            p_bar.progress = percent
            file_lbl.update(f"[yellow]{error}[/yellow]")
            _finish()
            return

        if error:
            # fatal abort, or an error from a non-restore flow (deploy)
            self.is_done = True
            spinner_lbl.update("[bold red]❌ Error Encountered[/bold red]")
            p_bar.progress = 0
            file_lbl.update(f"[bold red]{error}[/bold red]")
            _finish()
            return

        if done:
            self.is_done = True
            spinner_lbl.update("[bold yellow]✅ Operation completed successfully![/bold yellow]")
            p_bar.progress = 100
            file_lbl.update("All files restored and verified.")
            _finish()
            return

        spinner_char = SPINNER_FRAMES[spinner_idx % len(SPINNER_FRAMES)]
        spinner_lbl.update(f"{spinner_char} {self.title_text} [ {percent}% ]")
        p_bar.progress = percent
        file_lbl.update(current_file)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-done":
            self.dismiss(None)
        elif event.button.id == "btn-cancel":
            self.trigger_cancel()


class ConfirmExitModal(ModalScreen[bool]):
    """Confirmation modal before quitting the application."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("q", "confirm", "Quit"),
        Binding("y", "confirm", "Yes"),
    ]

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def compose(self) -> ComposeResult:
        with Vertical(id="modal-dialog"):
            yield Label("⚠️  QUIT BTRFS RESTORE TUI  ⚠️", id="modal-title")
            with Vertical(id="modal-content"):
                yield Label("Are you sure you want to quit the application?", id="exit-msg")
            with Horizontal(id="modal-buttons"):
                yield Button("<Q> Quit", id="btn-exit-yes", variant="warning")
                yield Button("<C> Cancel (Esc)", id="btn-exit-no", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-exit-yes":
            self.dismiss(True)
        else:
            self.dismiss(False)


class BtrfsRestoreApp(App):
    """Main Btrfs Restore TUI application."""

    CSS = RETRO_CSS
    TITLE = "Btrfs Restore TUI (AGY Time Explorer)"
    BINDINGS = [
        Binding("space", "toggle_select", "Select/Unselect", priority=True),
        Binding("r", "restore_original", "Restore Original"),
        Binding("e", "extract_custom", "Extract to..."),
        Binding("s", "switch_snapshot", "Switch Snapshot"),
        Binding("a", "select_all", "Select All"),
        Binding("q", "quit_app", "Quit"),
    ]

    def __init__(self):
        super().__init__()
        self.scanner = SnapshotScanner()
        self.engine = RestoreEngine()
        self.snapshots: List[SnapshotInfo] = []
        self.current_snapshot: Optional[SnapshotInfo] = None
        self.selected_paths: Set[Path] = set()
        self.node_path_map: Dict[str, Path] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="snapshot-bar")
        with Container(id="tree-container"):
            yield Tree("Snapshot Explorer", id="file-tree")
        yield Static(id="status-box")
        with Horizontal(id="action-bar"):
            yield Button(Content("[ <R> Restore to Original ]"), id="btn-restore", variant="primary")
            yield Button(Content("[ <E> Extract to... ]"), id="btn-extract")
            yield Button(Content("[ <S> Switch Snapshot ]"), id="btn-switch")
            yield Button(Content("[ <Q> Quit ]"), id="btn-quit")
        yield Footer()

    def on_mount(self) -> None:
        # Foolproof staging cleanup: atexit + on_unmount handle it. The signal
        # handlers only unwind (SystemExit) - which runs both - so nothing
        # unsafe happens in signal context.
        atexit.register(self.engine._cleanup_own_staging)
        try:
            signal.signal(signal.SIGTERM, _unwind_on_signal)
            signal.signal(signal.SIGHUP, _unwind_on_signal)
        except (ValueError, OSError):
            pass

        # Purge staging left by dead runs on startup (keep a parallel run's)
        self.engine.cleanup_staging(orphans_only=True)
        self.refresh_snapshots()

    def refresh_snapshots(self) -> None:
        # Manifest-based backup-now snapshots first, then legacy local/USB scans.
        self.snapshots = (
            self.scanner.scan_target_snapshots()
            + self.scanner.scan_local_snapshots()
            + self.scanner.scan_usb_snapshots()
        )
        seen, deduped = set(), []
        for s in self.snapshots:
            if s.id not in seen:
                seen.add(s.id)
                deduped.append(s)
        self.snapshots = deduped
        if self.snapshots:
            self.current_snapshot = next(
                (s for s in self.snapshots if "home" in s.id.lower()),
                self.snapshots[0],
            )
            self.load_snapshot_tree()
        else:
            self.query_one("#snapshot-bar", Static).update("❌ No Btrfs snapshots found in /.snapshots or USB")

        # Scan remote server in background so UI never hangs
        self.scan_remote_background()

    @work(thread=True)
    def scan_remote_background(self) -> None:
        """Scan remote server asynchronously in background thread, and always
        report the outcome so a silent 'nothing new' isn't mistaken for
        'never connected'."""
        name = self.scanner.config.remote_name or "Remote"
        try:
            remote_snaps = self.scanner.scan_remote_snapshots()
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self.notify, f"⚠️ {name}: {exc}", severity="warning")
            return

        status = getattr(self.scanner, "remote_status", "none")
        skipped = getattr(self.scanner, "remote_skipped_local", 0)

        if status == "none":
            return  # no remote configured - stay quiet
        if status == "unreachable":
            self.app.call_from_thread(
                self.notify, f"⚠️ {name} not responding (check network / SSH / config)",
                severity="warning")
            return
        if status == "no_privilege":
            self.app.call_from_thread(
                self.notify,
                f"⚠️ {name} connected, but 'sudo btrfs' is not allowed there - "
                "listing what's readable without root only",
                severity="warning")

        existing = {s.id for s in self.snapshots}
        added = [r for r in remote_snaps if r.id not in existing]
        if added:
            self.snapshots.extend(added)
            self.snapshots.sort(key=lambda s: s.timestamp, reverse=True)
            self.app.call_from_thread(
                self.notify, f"📡 {name}: {len(added)} snapshot(s) only on the remote",
                severity="information")
        elif skipped:
            self.app.call_from_thread(
                self.notify,
                f"📡 {name} connected — its {skipped} snapshot(s) are already local",
                severity="information")
        else:
            self.app.call_from_thread(
                self.notify, f"📡 {name} connected — no snapshots",
                severity="information")

    def load_snapshot_tree(self) -> None:
        if not self.current_snapshot:
            return

        snap = self.current_snapshot
        bar_text = (
            f"SOURCE: [bold yellow][{snap.snap_type.value}][/bold yellow] {snap.name} | "
            f"Date: [bold cyan]{snap.formatted_time}[/bold cyan] | "
            f"Path: [dim]{snap.path}[/dim]  (<S> Switch)"
        )
        self.query_one("#snapshot-bar", Static).update(bar_text)

        tree = self.query_one("#file-tree", Tree)
        tree.clear()
        tree.root.label = f"📁 {snap.path.name}/"
        tree.root.data = {"path": snap.path, "loaded": False, "is_dir": True}
        self.node_path_map.clear()
        self.selected_paths.clear()

        # Populate root level lazily
        self._populate_node(tree.root)
        tree.root.expand()
        tree.focus()
        self.update_status_bar()

    def _format_node_label(self, path: Path, is_dir: bool, is_selected: bool) -> Text:
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

        if is_selected:
            return Text.assemble(
                ("[X] ", "bold yellow"),
                ("📁 " if is_dir else "📄 ", "bold yellow"),
                (path.name + ("/" if is_dir else ""), "bold yellow"),
                (size_str, "dim yellow"),
            )
        else:
            return Text.assemble(
                ("[ ] ", "green"),
                ("📁 " if is_dir else "📄 ", "bold green"),
                (path.name + ("/" if is_dir else ""), "green"),
                (size_str, "dim green"),
            )

    def _populate_node(self, node: TreeNode) -> None:
        if node.data.get("loaded"):
            return

        path: Path = node.data["path"]
        if not path.is_dir():
            return

        try:
            entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            node.add_leaf(Text("⚠️ [Permission denied]", "bold red"))
            node.data["loaded"] = True
            return

        for entry in entries:
            is_dir = entry.is_dir()
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
        if not node or not node.data:
            return

        path: Path = node.data.get("path")
        if not path or path == self.current_snapshot.path:
            return

        is_dir = node.data.get("is_dir", False)
        if path in self.selected_paths:
            self.selected_paths.remove(path)
            is_selected = False
        else:
            self.selected_paths.add(path)
            is_selected = True

        node.label = self._format_node_label(path, is_dir, is_selected)
        self.update_status_bar()

    def action_select_all(self) -> None:
        """Select or deselect all items in the current directory."""
        tree = self.query_one("#file-tree", Tree)
        current = tree.cursor_node
        parent = current.parent if current and current.parent else tree.root

        all_selected = True
        for child in parent.children:
            p = child.data.get("path")
            if p and p not in self.selected_paths:
                all_selected = False
                break

        for child in parent.children:
            p = child.data.get("path")
            if not p:
                continue
            is_dir = child.data.get("is_dir", False)
            if all_selected:
                self.selected_paths.discard(p)
                child.label = self._format_node_label(p, is_dir, False)
            else:
                self.selected_paths.add(p)
                child.label = self._format_node_label(p, is_dir, True)

        self.update_status_bar()

    def update_status_bar(self) -> None:
        count = len(self.selected_paths)
        status_box = self.query_one("#status-box", Static)

        if count == 0:
            status_box.update(
                "⚪ No items selected. "
                "Use [bold yellow][Space][/bold yellow] to mark/unmark, "
                "[bold yellow][Enter][/bold yellow] to expand/collapse folders."
            )
            return

        items = self.engine.prepare_items(list(self.selected_paths), self.current_snapshot.path)
        total_size = sum(i.size_bytes for i in items)

        if total_size < 1024:
            size_str = f"{total_size} B"
        elif total_size < 1024 * 1024:
            size_str = f"{total_size / 1024:.1f} KB"
        elif total_size < 1024 * 1024 * 1024:
            size_str = f"{total_size / (1024 * 1024):.2f} MB"
        else:
            size_str = f"{total_size / (1024 * 1024 * 1024):.2f} GB"

        status_box.update(
            f"🟡 [bold yellow]{count} items marked[/bold yellow] ({size_str} total). "
            f"Press [bold yellow]<R>[/bold yellow] to Restore Original or "
            f"[bold yellow]<E>[/bold yellow] to Extract to custom path."
        )

    def action_switch_snapshot(self) -> None:
        cur_id = self.current_snapshot.id if self.current_snapshot else None

        def on_select(selected: Optional[SnapshotInfo]):
            if not selected:
                return

            if selected.snap_type == SnapshotType.LOCAL or (selected.snap_type == SnapshotType.USB and selected.is_subvolume):
                self.current_snapshot = selected
                self.load_snapshot_tree()
                return

            # For remote (i7server) or legacy flat USB archives, deploy into staging
            self.deploy_and_switch(selected)

        self.push_screen(SnapshotSelectModal(self.snapshots, cur_id), on_select)

    @work(thread=True)
    def deploy_and_switch(self, snapshot: SnapshotInfo) -> None:
        """Deploy remote or USB snapshot stream in background with progress modal."""
        cancelled = False
        done_flag = False

        def on_cancel_deploy():
            nonlocal cancelled, done_flag
            cancelled = True
            done_flag = True
            self.engine.cancel_active_operation()
            self.notify("Deployment cancelled. Staging cleaned up.", severity="warning")

        progress_modal = ProgressModal(
            title=f"Deploying {snapshot.name}...",
            can_cancel=True,
            on_cancel=on_cancel_deploy,
        )
        self.app.call_from_thread(self.push_screen, progress_modal)

        # Active spinner while streaming
        spinner_idx = 0

        def spin_worker():
            nonlocal spinner_idx
            while not done_flag:
                spinner_idx = (spinner_idx + 1) % 4
                self.app.call_from_thread(
                    progress_modal.update_progress,
                    spinner_idx,
                    50,
                    "Receiving Btrfs stream into /.snapshots/staging...",
                )
                import time
                time.sleep(0.15)

        import threading
        t = threading.Thread(target=spin_worker, daemon=True)
        t.start()

        try:
            staging_path = self.engine.deploy_staging(snapshot)
            if cancelled:
                return

            done_flag = True
            t.join(timeout=0.5)

            snapshot.path = staging_path
            snapshot.is_subvolume = True
            self.current_snapshot = snapshot

            self.app.call_from_thread(
                progress_modal.update_progress,
                0,
                100,
                f"Mounted at {staging_path}",
                done=True,
            )
            import time
            time.sleep(0.5)
            self.app.call_from_thread(progress_modal.dismiss, None)
            self.app.call_from_thread(self.load_snapshot_tree)
            self.notify(f"Loaded snapshot: {snapshot.name}", severity="information")
        except Exception as e:
            if cancelled:
                return
            done_flag = True
            t.join(timeout=0.5)
            self.app.call_from_thread(
                progress_modal.update_progress,
                0,
                0,
                "",
                error=str(e),
            )

    def action_restore_original(self) -> None:
        if not self.selected_paths:
            self.notify("Select at least one file or folder first (Space)", severity="warning")
            return

        # Target base path determination
        if self.current_snapshot.id == "local_home_parent" or "Home" in self.current_snapshot.name:
            user = os.getenv("SUDO_USER") or os.getenv("USER") or getpass.getuser()
            target_base = Path(f"/home/{user}")
        elif self.current_snapshot.id == "local_root_parent" or "Root" in self.current_snapshot.name:
            target_base = Path("/")
        else:
            user = os.getenv("SUDO_USER") or os.getenv("USER") or getpass.getuser()
            target_base = Path(f"/home/{user}")

        items = self.engine.prepare_items(list(self.selected_paths), self.current_snapshot.path)
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

        user = os.getenv("SUDO_USER") or os.getenv("USER") or getpass.getuser()
        default_dir = Path(f"/home/{user}") / f"recovered_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        items = self.engine.prepare_items(list(self.selected_paths), self.current_snapshot.path)

        def on_path(target_path: Optional[Path]):
            if target_path:
                def on_confirm(resolution: Optional[ConflictResolution]):
                    if resolution:
                        self.run_restoration(items, target_path, resolution)
                self.push_screen(ConfirmRestoreModal(items, target_path), on_confirm)

        self.push_screen(CustomPathModal(default_dir), on_path)

    @work(thread=True)
    def run_restoration(self, items: List[RestoreItem], target_base: Path, resolution: ConflictResolution):
        progress_modal = ProgressModal(title="Restoring files...")
        self.app.call_from_thread(self.push_screen, progress_modal)

        gen = self.engine.restore_generator(items, target_base, resolution)
        for state in gen:
            self.app.call_from_thread(
                progress_modal.update_progress,
                state.spinner_idx,
                state.percent,
                state.current_file,
                state.done,
                state.error,
                state,
            )
            import time
            time.sleep(0.03)

    def action_quit_app(self) -> None:
        """Prompt confirmation before quitting."""
        def on_exit_confirm(confirmed: Optional[bool]):
            if confirmed:
                self.exit()

        self.push_screen(ConfirmExitModal(), on_exit_confirm)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-restore":
            self.action_restore_original()
        elif event.button.id == "btn-extract":
            self.action_extract_custom()
        elif event.button.id == "btn-switch":
            self.action_switch_snapshot()
        elif event.button.id == "btn-quit":
            self.action_quit_app()

    def on_unmount(self) -> None:
        """Cleanup this run's staging subvolume on exit."""
        try:
            self.engine._cleanup_own_staging()
        except Exception:
            pass


if __name__ == "__main__":
    app = BtrfsRestoreApp()
    app.run()
