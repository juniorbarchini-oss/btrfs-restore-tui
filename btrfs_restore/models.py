"""
Data models for Btrfs Restore TUI.
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import List, Optional


class SnapshotType(str, Enum):
    LOCAL = "LOCAL"
    USB = "USB"
    REMOTE = "REMOTE"


class ConflictResolution(str, Enum):
    OVERWRITE = "overwrite"
    BACKUP = "backup"
    SKIP = "skip"
    CANCEL = "cancel"


@dataclass
class SnapshotInfo:
    id: str
    name: str
    path: Path
    snap_type: SnapshotType
    timestamp: datetime
    description: str = ""
    is_subvolume: bool = True
    size_bytes: Optional[int] = None
    status: str = "completed"          # completed | partial | unknown
    manifest: Optional[dict] = None    # the backup manifest.json, when present

    @property
    def formatted_time(self) -> str:
        return self.timestamp.strftime("%Y-%m-%d %H:%M:%S")

    @property
    def usable(self) -> bool:
        return self.status in ("completed", "partial", "unknown")


@dataclass
class RestoreItem:
    source_path: Path
    is_dir: bool
    size_bytes: int
    rel_path: Path  # Path relative to snapshot root (e.g. Documents/file.md)

    @property
    def formatted_size(self) -> str:
        if self.size_bytes < 1024:
            return f"{self.size_bytes} B"
        elif self.size_bytes < 1024 * 1024:
            return f"{self.size_bytes / 1024:.1f} KB"
        elif self.size_bytes < 1024 * 1024 * 1024:
            return f"{self.size_bytes / (1024 * 1024):.1f} MB"
        else:
            return f"{self.size_bytes / (1024 * 1024 * 1024):.2f} GB"


@dataclass
class RestorePlan:
    items: List[RestoreItem] = field(default_factory=list)
    target_base: Path = field(default_factory=Path.home)
    resolution: ConflictResolution = ConflictResolution.BACKUP

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.items)

    @property
    def total_items(self) -> int:
        return len(self.items)


@dataclass
class RestoreProgress:
    total_files: int
    processed_files: int
    total_bytes: int
    processed_bytes: int
    current_file: str
    spinner_idx: int = 0
    done: bool = False
    error: Optional[str] = None
    failed_files: int = 0
    errors: List[str] = field(default_factory=list)
    fatal: bool = False          # aborted before finishing (vs. some files failed)

    @property
    def percent(self) -> int:
        if self.done and not self.error:
            return 100
        if self.total_bytes == 0:
            return 100 if self.done else 0
        return min(100, int((self.processed_bytes / self.total_bytes) * 100))
