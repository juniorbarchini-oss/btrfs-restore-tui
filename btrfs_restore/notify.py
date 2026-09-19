"""Best-effort desktop alerts for backup-now.

backup-now runs as root (sudo) or from a timer, so the notification has to be
delivered to the real user's session bus. Never raises: an alert that cannot be
shown must not turn a finished backup into a crash. Same idea as the SharePoint
watcher: the same alert is not repeated within REPEAT seconds and the state is
cleared as soon as a backup completes.
"""
import hashlib
import os
import pwd
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

REPEAT = 3600


def _bus_path(uid: int) -> str:
    return f"/run/user/{uid}/bus"


def send_alert(title: str, body: str, user: str, state_file: Optional[Path],
               run: Callable = subprocess.run, now: Callable = time.time,
               exists: Callable = os.path.exists, is_root: Optional[bool] = None) -> bool:
    """Show a critical notification to `user`. True if it was sent."""
    try:
        key = hashlib.md5(f"{title}\n{body}".encode()).hexdigest()
        if state_file is not None:
            try:
                last_key, last_ts = state_file.read_text().split()
                if last_key == key and now() - float(last_ts) < REPEAT:
                    return False
            except (OSError, ValueError):
                pass
        root = (os.geteuid() == 0) if is_root is None else is_root
        cmd = ["notify-send", "-u", "critical", "-a", "backup-now", title, body[:300]]
        if root:
            uid = pwd.getpwnam(user).pw_uid
            bus = _bus_path(uid)
            if not exists(bus):
                return False                     # no graphical session for that user
            cmd = ["runuser", "-u", user, "--", "env",
                   f"DBUS_SESSION_BUS_ADDRESS=unix:path={bus}", *cmd]
        res = run(cmd, capture_output=True, timeout=10)
        if res.returncode != 0:
            return False
        if state_file is not None:
            try:
                state_file.write_text(f"{key} {now()}")
            except OSError:
                pass
        return True
    except (OSError, KeyError, subprocess.SubprocessError):
        return False


def clear_alert(state_file: Optional[Path]) -> None:
    try:
        if state_file is not None:
            state_file.unlink()
    except OSError:
        pass
