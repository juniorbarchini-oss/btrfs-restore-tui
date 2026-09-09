# Btrfs Restore TUI (AGY Time Explorer) - v1.0

> **A retro phosphor-green terminal UI for visual exploration and granular file/folder restoration from local and remote Btrfs snapshots.**
> Optimized for **Omarchy Quattro / Arch Linux**, and compatible with modern Linux distributions using Btrfs (Fedora, openSUSE, Debian/Ubuntu).

---

## 1. Overview and Purpose

In Linux environments utilizing Btrfs file systems, system and user backups are efficiently performed via atomic read-only snapshots and compressed streams (`btrfs send | zstd`).

While automated tools and scripts manage snapshot creation (such as `snapper` or custom backup scripts), recovering an individual deleted file or misplaced directory (e.g., an Obsidian note or a configuration file in `~/.config`) previously required tedious manual console commands (`btrfs receive`, mounting temporary subvolumes, `sudo` management, etc.).

**Btrfs Restore TUI** solves this by delivering an interactive, lightweight, keyboard-driven terminal application that emulates the ease of **Apple Time Machine** with the classic aesthetics of vintage UNIX VT100 phosphor-green serial terminals.

---

## 2. Key Features

* **Visual Snapshot Browser:**
  * **Local Snapshots:** Instant zero-latency browsing of local subvolumes (`/.snapshots/home_parent`, `root_parent`, and Snapper snapshots).
  * **Remote Snapshots (SSH / NAS / Backup Server):** Automatically detects compressed `.btrfs.zst` backup streams and native remote subvolumes over SSH.
  * **External Storage (USB Drives):** Automatically scans mounted USB drives (`/run/media/...`, `/media/...`, `/mnt/...`).
  * **Ephemeral Staging:** Automatically streams and mounts remote snapshots into a temporary Btrfs staging subvolume (`/.snapshots/staging/`) in the background, cleaning up cleanly on exit.
* **Retro Phosphor-Green Aesthetic:**
  * Deep black background (`#000000`) with luminous green text and double borders (`#00FF66`).
  * Marked items highlighted in vivid amber yellow (`#FFFF00`).
  * Real-time operation feedback with vintage ASCII spinner (`[ | ]`, `[ / ]`, `[ - ]`, `[ \ ]`) and percentage meter `[ XX% ]`.
* **Granular Keyboard Controls:**
  * `↑` / `↓`: Navigate directories and files.
  * `Enter`: Expand / Collapse directory nodes lazily.
  * `Space`: Toggle file/folder selection `[ ]` ➔ `[X]` in bright amber.
  * `a`: Select or deselect all items in the current folder.
  * `r`: Restore selected items directly to their original system locations.
  * `e`: Extract selected items to a custom directory (e.g., `~/recovered_...`).
  * `s`: Switch active snapshot (Local, USB, or Remote backup host).
  * `q`: Clean exit.
* **Safety and Permission Integrity:**
  * Conflict resolution dialogs: Backup existing files with `.bak` suffixes, overwrite, or cancel.
  * Preserves original user ownership (`$SUDO_USER` / current user) even when running with elevated Btrfs capabilities.

---

## 3. Keyboard Shortcuts Reference

| Key | Action | Description |
|---|---|---|
| `Space` | **Toggle Select** | Mark/unmark current file or directory `[X]` |
| `Enter` | **Expand / Collapse** | Open or close directory node in tree |
| `a` | **Select All** | Toggle selection for all items in active directory |
| `r` | **Restore Original** | Restore selected files to original locations |
| `e` | **Extract to...** | Extract to chosen directory with custom path modal |
| `s` | **Switch Snapshot** | Open modal to select Local, USB, or Remote snapshot |
| `Tab` | **Cycle Focus** | Move focus between file tree and action bar buttons |
| `Esc` | **Cancel / Back** | Close active modal dialog |
| `q` | **Quit** | Clean staging subvolumes and exit |

---

## 4. Architecture and Stack

* **Language:** Python 3
* **TUI Framework:** `Textual` (native async terminal UI with truecolor support)
* **Backend:** Btrfs ioctl utilities, `zstd`, OpenSSH client
* **Deployment Standard (Ecosystem Rule 7):**
  * Binary & source location: `/opt/btrfs-restore-tui/`
  * Global executable symlink: `/usr/local/bin/restore-now`
  * Desktop application entry: `/usr/share/applications/btrfs-restore.desktop` (launches in `foot`)

---

---

## 5. Configuration (Optional Remote Server)

By default, **Btrfs Restore TUI** immediately scans local snapshots and mounted USB drives without configuration.

To enable remote SSH snapshot browsing, create a configuration file at `~/.config/btrfs-restore/config.conf` (or `/etc/btrfs-restore/config.conf`):

```conf
# ~/.config/btrfs-restore/config.conf
BTRFS_REMOTE_HOST=192.168.1.100
BTRFS_REMOTE_PATH=/mnt/backups
BTRFS_REMOTE_USER=user
BTRFS_REMOTE_NAME=BackupServer
BTRFS_REMOTE_PORT=22
```

Alternatively, environment variables can be set (`BTRFS_REMOTE_HOST`, `BTRFS_REMOTE_PATH`, `BTRFS_REMOTE_USER`, `BTRFS_REMOTE_NAME`).

---

## 6. Installation and Usage

### Requirements
* Arch Linux / Omarchy Quattro:
  ```bash
  sudo pacman -S python-textual btrfs-progs zstd pv openssh
  ```
* Debian / Ubuntu (Btrfs root):
  ```bash
  sudo apt install python3-textual btrfs-progs zstd pv openssh-client
  ```
* Fedora:
  ```bash
  sudo dnf install python3-textual btrfs-progs zstd pv openssh-clients
  ```

### Installation
From the cloned repository:
```bash
sudo ./install.sh
```

### Running
From any terminal:
```bash
restore-now
```
Or launch **Btrfs Restore TUI** directly from your application launcher.

---

## 7. License
MIT License. Created for the AGY Ecosystem.
