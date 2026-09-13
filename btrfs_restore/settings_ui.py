"""
Settings screen for Btrfs Restore TUI.

Lets the user pick a backup destination and configure the SSH remote target
without hand-editing ~/.config/restore-tui/config.conf. Runs unprivileged -
it only reads mount info and writes the user's own config file, no root
needed - and is a normal Textual App, not a modal, so it can run standalone
(`restore-tui settings`) as well as from the main menu.
"""
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, Footer, Header, Input, Label, RadioButton, RadioSet, Static

from .config import Config, SUPPORTED_TARGET_FSTYPES, list_backup_drives, save_user_settings
from .theme import RETRO_CSS

_AUTO_ID = "auto-detect"


class SettingsApp(App):
    """Retro Settings screen: backup destination + SSH remote target."""

    CSS = RETRO_CSS
    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("s", "save", "Save"),
    ]

    def __init__(self):
        super().__init__()
        self.cfg = Config.load()
        self.drives: List[Tuple[Path, Optional[str]]] = list_backup_drives(self.cfg.user)
        self.saved = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with VerticalScroll(id="settings-body"):
            yield Static("BACKUP DESTINATION (USB / external drive)", classes="settings-section")
            if self.drives:
                yield Static(
                    "Pick which connected drive to use. A drive not formatted "
                    "ext4 or btrfs is shown but flagged - format it with your "
                    "usual disk manager before selecting it.",
                    classes="settings-hint",
                )
                with RadioSet(id="target-radio"):
                    yield RadioButton(
                        "Auto-detect (no pin - use whatever is plugged in)",
                        id=_AUTO_ID,
                        value=self.cfg.target_root is None,
                    )
                    for drive, fstype in self.drives:
                        label = self._drive_label(drive, fstype)
                        is_current = (
                            self.cfg.target_root is not None
                            and Path(self.cfg.target_root).parent == drive
                        )
                        yield RadioButton(label, id=self._drive_id(drive), value=is_current)
            else:
                yield Static(
                    "No removable drives detected right now. Plug one in and "
                    "reopen Settings - it needs to be formatted ext4 or btrfs.",
                    classes="settings-hint",
                )

            yield Static("REMOTE (SSH) BACKUP TARGET", classes="settings-section")
            yield Checkbox(
                "Enable SSH remote target",
                value=bool(self.cfg.remote_host),
                id="remote-enabled",
            )
            with Vertical(id="settings-remote-fields"):
                yield Label("Host (IP or hostname)")
                yield Input(value=self.cfg.remote_host, id="input-host", placeholder="e.g. 100.81.31.97")
                yield Label("User")
                yield Input(value=self.cfg.remote_user, id="input-user", placeholder=self.cfg.user)
                yield Label("Remote path")
                yield Input(value=self.cfg.remote_path, id="input-path", placeholder="/mnt/backups")
                yield Label("Port")
                yield Input(value=str(self.cfg.remote_port), id="input-port", placeholder="22")
                yield Label("Display name")
                yield Input(value=self.cfg.remote_name, id="input-name", placeholder="Remote")

            yield Static("", id="settings-status")
        with Vertical(id="modal-buttons"):
            yield Button("<S> Save", id="btn-save", variant="primary")
            yield Button("<Esc> Cancel", id="btn-cancel", variant="default")
        yield Footer()

    def on_mount(self) -> None:
        self._sync_remote_fields_enabled()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "remote-enabled":
            self._sync_remote_fields_enabled()

    def _sync_remote_fields_enabled(self) -> None:
        enabled = self.query_one("#remote-enabled", Checkbox).value
        fields = self.query_one("#settings-remote-fields")
        fields.set_class(not enabled, "disabled")
        for input_id in ("#input-host", "#input-user", "#input-path", "#input-port", "#input-name"):
            self.query_one(input_id, Input).disabled = not enabled

    @staticmethod
    def _drive_id(drive: Path) -> str:
        return "drive-" + str(drive).replace("/", "_")

    def _drive_label(self, drive: Path, fstype: Optional[str]) -> str:
        if fstype in SUPPORTED_TARGET_FSTYPES:
            return f"{drive}  [{fstype}]"
        shown = fstype or "unknown"
        return f"{drive}  [{shown} - format as ext4 or btrfs to use this drive]"

    def _selected_drive(self) -> Optional[Path]:
        if not self.drives:
            return None
        radio = self.query_one("#target-radio", RadioSet)
        pressed = radio.pressed_button
        if pressed is None or pressed.id == _AUTO_ID:
            return None
        for drive, _ in self.drives:
            if self._drive_id(drive) == pressed.id:
                return drive
        return None

    def action_cancel(self) -> None:
        self.exit(False)

    def action_save(self) -> None:
        self._save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-save":
            self._save()
        else:
            self.exit(False)

    def _save(self) -> None:
        status = self.query_one("#settings-status", Static)
        remote_enabled = self.query_one("#remote-enabled", Checkbox).value
        host = self.query_one("#input-host", Input).value.strip()

        if remote_enabled and not host:
            status.update("[bold red]Enter a host before enabling the SSH remote target.[/]")
            return

        port_raw = self.query_one("#input-port", Input).value.strip()
        try:
            port = int(port_raw) if port_raw else 22
        except ValueError:
            status.update("[bold red]Port must be a number.[/]")
            return

        drive = self._selected_drive()
        updates = {
            "TARGET_DIR": str(drive) if drive else "",
            "REMOTE_HOST": host if remote_enabled else "",
            "REMOTE_USER": self.query_one("#input-user", Input).value.strip(),
            "REMOTE_PATH": self.query_one("#input-path", Input).value.strip(),
            "REMOTE_PORT": str(port),
            "REMOTE_NAME": self.query_one("#input-name", Input).value.strip() or "Remote",
        }
        path = save_user_settings(updates)
        status.update(f"[bold #00FF66]Saved to {path}[/]")
        self.saved = True
        self.set_timer(0.8, lambda: self.exit(True))


def main() -> int:
    app = SettingsApp()
    app.run()
    return 0 if app.saved else 1


if __name__ == "__main__":
    sys.exit(main())
