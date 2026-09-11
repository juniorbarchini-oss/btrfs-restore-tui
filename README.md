# Btrfs Restore TUI

> **One terminal app for Btrfs backup *and* granular restore.** A retro
> phosphor-green TUI to browse and recover individual files/folders from local,
> USB and SSH snapshots, plus an incremental `btrfs send`-based backup engine.
> Built for **Omarchy / Arch Linux**; works on any Btrfs-root distro.

Status: **v1.0** released (restore only). `develop` carries **Phase A** — the
backup engine and the unified launcher, reaching parity with the ext4 sibling
[`restore-tui`](https://github.com/juniorbarchini-oss/restore-tui). Not yet
tagged.

---

## 1. What it does

| Command | Purpose |
|---|---|
| `restore-tui` | Menu: **[B]** Backup now · **[R]** Restore files/folders · **[F]** Full recovery · **[Q]** Quit |
| `backup-now` | Incremental Btrfs backup of `/` and `/home` to a USB drive and/or an SSH host |
| `restore-now` | The retro TUI to browse a snapshot and restore/extract files |
| `restore-tui --gc` | Remove staging / scratch left by a killed run; report freed space |
| `restore-tui --paths` | Print every path the tool creates or touches |
| `sudo ./uninstall.sh` | Remove everything (keeps your config and snapshots unless `--purge`) |

Privilege is deferred: the menu runs unprivileged; picking Backup or Restore
re-execs that one command under `sudo`.

---

## 2. Backup engine (`backup-now`)

Read-only local snapshots of each source subvolume, then `btrfs send [-p] | zstd`
to the target(s) — incremental whenever a shared parent exists.

**Layout written on the target** (same shape as the ext4 sibling, for a future
merge):

```
<target>/btrfs-restore/
├── snapshots/<YYYY-MM-DD_HHMMSS>/
│   ├── root_<ts>/  home_<ts>/   received subvolumes (btrfs target)
│   ├── root.btrfs.zst  ...       compressed streams (non-btrfs target)
│   ├── _system_state/            pacman/AUR/flatpak lists, disk layout, boot
│   ├── restore.sh                self-contained bare-metal recovery (one snapshot)
│   ├── manifest.json             status: completed | partial | failed
│   └── backup.log
├── latest -> snapshots/<ts>      only ever points at a `completed` one
├── disaster-recovery.sh          pick a snapshot, run its restore.sh
├── RECOVERY.md                   plain-language recovery instructions
└── btrfs-restore-tui-src.tar.gz  the app, for an offline reinstall
```

The SSH host receives native subvolumes under `<remote>/{root,home}/<name>` and
the system state under `<remote>/meta/<name>/`.

**Safety.** A run that cannot finish cleanly is marked `partial`/`failed`,
`latest` is left untouched, and its snapshot is never used as an incremental
parent. All transient state for a run lives under one pid-named dir
(`/.snapshots/.backup-tmp/<pid>-<ts>/`), swept on the next start and on a
`SIGTERM`; a hard kill leaves at most that one directory. A stalled SSH link
times out instead of hanging the backup.

**Exclusions.** `btrfs send` cannot skip paths mid-stream, so the engine takes a
*writable* snapshot, deletes throwaway paths (caches, trash, crash dumps), then
flips it read-only and sends that — the cleaned snapshot is also the incremental
parent, so the churn never re-enters a later delta. A built-in list is always
applied (`/var/tmp`, coredumps, `~/.cache`, `~/.local/share/Trash`, browser and
Electron `*Cache` dirs, …). Add your own with `EXCLUDE=` lines in the config, or
turn the built-in list off with `EXCLUDE_DEFAULTS=off`.

**Retention.** After a successful backup the oldest `completed` snapshots on the
target drive are pruned until it is back under `MAX_DISK_PERCENT` (default 80),
never below `MIN_KEEP` (2), never the last. `MAX_SNAPSHOTS` is an optional hard
cap. Local RO snapshots are kept `LOCAL_KEEP` deep (10) as `send -p` parents.

Flags: `--dry-run`, `--target DIR`, `--keep N`, `--min-keep N`,
`--max-disk-percent PCT`, `--quiet`.

---

## 3. Restore TUI (`restore-now`)

Browse a snapshot as a file tree and restore selected files/folders to their
original location or extract them elsewhere.

| Key | Action |
|---|---|
| `↑` `↓` | Navigate |
| `Enter` | Expand / collapse a directory |
| `Space` | Toggle selection `[ ]` ↔ `[X]` |
| `a` | Select / deselect all in the current folder |
| `r` | Restore selected items to their original path |
| `e` | Extract selected items to a chosen directory |
| `s` | Switch snapshot (Local / USB / Remote) |
| `q` | Unmount / clean staging and quit |

**Sources.** Local subvolumes and Snapper snapshots (instant), mounted USB
drives, and an SSH host. A remote snapshot is first mounted **read-only over
SSHFS** (`/.snapshots/.remote-mnt/<pid>-<slug>/`) so you can browse it and pull
only the files you mark — nothing else crosses the network. If SSHFS is
unavailable, or you need the whole subvolume back, it falls back to streaming it
into a validated Btrfs staging subvolume (`/.snapshots/staging/<pid>-<slug>/`,
or `STAGING_DIR`) with a live byte/rate readout. Both are cleaned up on exit and
swept on the next start after a killed run.

**Conflicts.** When a file already exists: `<B>` back it up as `.bak`, `<O>`
overwrite, `<S>` skip existing, `<C>` cancel.

**Ownership.** Restoring into your home → files are owned by you. Restoring to
`/` or another system path → each file keeps the owner recorded in the snapshot
(so `/etc/sudoers` stays `root:root`); the confirm dialog flags a system
restore.

**Errors are honest.** A per-file failure is collected and the run continues;
the final state is *"Restored with errors (X/Y ok, Z failed)"* with the list —
never a bare "completed". A fatal condition (unwritable target, disk full)
aborts at once.

**Empty directories** in a selection are recreated.

---

## 4. Bare-metal recovery

Each backup writes a recovery kit at the **root of the backup folder** — no
Python, no network, needs only bash, coreutils, rsync, pacman, btrfs-progs,
zstd. From a fresh Arch install / live ISO:

```bash
mount <drive>
cd <drive>/btrfs-restore
./disaster-recovery.sh              # lists snapshots, pick one, runs its restore.sh
./disaster-recovery.sh --root /mnt  # from a live ISO, new root mounted at /mnt
```

`disaster-recovery.sh` just drives the chosen snapshot's own `restore.sh`
(`snapshots/<ts>/restore.sh`), which replays pacman config + mirrors, explicit
and AUR packages, Flatpaks, `/etc` bits, systemd units and the home tree. Read
`RECOVERY.md` for the full walkthrough. To recover only a few files, reinstall
from `btrfs-restore-tui-src.tar.gz` and use `restore-now`.

---

## 5. Configuration

No config needed for local + USB. For the SSH target/source, create
`~/.config/btrfs-restore/config.conf` (see `config.conf.example`):

```conf
REMOTE_HOST=192.168.1.100
REMOTE_PATH=/mnt/backups
REMOTE_USER=user
REMOTE_NAME=BackupServer
REMOTE_PORT=22

# STAGING_DIR=/.snapshots/staging      # must be on a btrfs mount
# EXCLUDE=*/node_modules               # extra backup exclusions, one per line
# EXCLUDE_DEFAULTS=off                 # drop the built-in exclusion list
# MAX_DISK_PERCENT=80  MIN_KEEP=2  LOCAL_KEEP=10
```

Every key also works as an environment variable with the `RESTORE_TUI_` prefix
(env wins). The old `BTRFS_REMOTE_*` names still work but are deprecated. The
file is never rewritten by the program.

**SSH host requirement.** The remote user must be able to run `btrfs` on
`REMOTE_PATH`. Either it owns the directory, or add a sudoers line on the host:

```
<user> ALL=(root) NOPASSWD: /usr/bin/btrfs
```

---

## 6. Install

```bash
# System tools the app shells out to (Python packages are NOT needed system-wide):
sudo pacman -S btrfs-progs zstd pv openssh sshfs          # Arch / Omarchy
# Debian/Ubuntu (btrfs root):  apt install btrfs-progs zstd pv openssh-client sshfs
# Fedora:                      dnf install btrfs-progs zstd pv openssh-clients fuse-sshfs

sudo ./install.sh          # -> /opt/btrfs-restore-tui, symlinks in /usr/local/bin, .desktop
```

`install.sh` builds a self-contained `/opt/btrfs-restore-tui/.venv` with
`textual` + `rich` (falling back to system Python only if `venv` is
unavailable), installs `sshfs` when missing, and retires a pre-existing loose
`~/.local/bin/backup-now` (the old ad-hoc script this engine replaces) — archiving a
copy to `~/.config/btrfs-restore/legacy-backup-now.sh.bak` and taking it off
`PATH`.

`sudo ./uninstall.sh` reverses it (`--yes` = app only, `--purge` = also config
and the legacy script; snapshots always need a typed `DELETE`).

---

## 7. Stack

Python 3 · `textual` (TUI) · `rich` (backup dashboard), both in a bundled venv ·
`btrfs-progs`, `zstd`, `pv`, OpenSSH, `sshfs` (optional, for lightweight remote
browse). Deployed to `/opt/btrfs-restore-tui/`; commands
`restore-tui` / `backup-now` / `restore-now` in `/usr/local/bin/`.

**Tests.** `./run-tests.sh` (bootstraps `.venv` from `requirements-dev.txt`,
runs `pytest`). `sudo ./run-tests.sh --e2e` adds the loop-device end-to-end
test that exercises real `btrfs send`/`receive`.

---

## 8. License

MIT.
