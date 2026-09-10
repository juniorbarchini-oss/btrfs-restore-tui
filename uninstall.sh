#!/usr/bin/env bash
# ==============================================================================
# Uninstaller for Btrfs Restore TUI (AGY Time Machine)
#
# Removes everything install.sh put on the machine. Leaves user config and your
# backup snapshots alone unless you explicitly confirm.
#
#   sudo ./uninstall.sh            interactive
#   sudo ./uninstall.sh --yes      remove the app, keep all user data, no prompts
#   sudo ./uninstall.sh --purge    also remove config + legacy script (still
#                                  asks before touching snapshots)
# ==============================================================================
set -euo pipefail

APP_NAME="btrfs-restore-tui"
INSTALL_DIR="/opt/${APP_NAME}"
DESKTOP_ENTRY="/usr/share/applications/btrfs-restore.desktop"
REAL_USER="${SUDO_USER:-$(id -un)}"
REAL_HOME="$(getent passwd "${REAL_USER}" | cut -d: -f6)"
SNAP_DIR="/.snapshots"

ASSUME_YES=0
PURGE=0
for a in "$@"; do
    case "$a" in
        --yes|-y) ASSUME_YES=1 ;;
        --purge)  PURGE=1 ;;
        *) echo "unknown option: $a"; exit 2 ;;
    esac
done

if [ "$(id -u)" -ne 0 ]; then
    echo "[!] needs root - re-running with sudo"
    exec sudo "$0" "$@"
fi

_ask() {  # _ask "question"  -> 0 if yes
    [ "${ASSUME_YES}" -eq 1 ] && return 1   # --yes means "app only", not "purge everything"
    read -r -p "$1 [y/N] " ans
    [[ "${ans}" =~ ^[Yy]$ ]]
}

echo "=== Uninstalling ${APP_NAME} ==="

# 1. staging / scratch from any run (best effort, before we delete the code)
if [ -x /usr/local/bin/restore-tui ]; then
    /usr/local/bin/restore-tui --gc || true
fi
for d in "${SNAP_DIR}"/staging/* "${SNAP_DIR}"/.backup-tmp/*; do
    [ -e "$d" ] || continue
    btrfs subvolume delete "$d" 2>/dev/null || rm -rf "$d"
done

# 2. the app itself
echo "--> ${INSTALL_DIR}"
rm -rf "${INSTALL_DIR:?}"
echo "--> /usr/local/bin/{restore-tui,restore-now,backup-now} (ours only)"
for link in restore-tui restore-now backup-now; do
    tgt="/usr/local/bin/${link}"
    if [ -L "${tgt}" ] && [[ "$(readlink -f "${tgt}" 2>/dev/null)" == "${INSTALL_DIR}"* ]]; then
        rm -f "${tgt}"
    fi
done
echo "--> ${DESKTOP_ENTRY}"
rm -f "${DESKTOP_ENTRY}"

# 3. user config
if [ "${PURGE}" -eq 1 ] || _ask "Also remove user config in ${REAL_HOME}/.config/{btrfs-restore,restore-tui}/ ?"; then
    rm -rf "${REAL_HOME}/.config/btrfs-restore" "${REAL_HOME}/.config/restore-tui"
    echo "    removed user config"
fi

# 4. legacy AGY backup-now script
LEGACY="${REAL_HOME}/.local/bin/backup-now"
if [ -f "${LEGACY}" ] && { [ "${PURGE}" -eq 1 ] || _ask "Remove the legacy script ${LEGACY} ?"; }; then
    rm -f "${LEGACY}"
    echo "    removed ${LEGACY}"
fi

# 5. snapshots - never without a typed confirmation
if [ -d "${SNAP_DIR}" ] && compgen -G "${SNAP_DIR}/{root,home}_*" >/dev/null; then
    echo
    echo "  Local snapshots in ${SNAP_DIR}:"
    ls -d "${SNAP_DIR}"/{root,home}_* 2>/dev/null | sed 's/^/    /'
    echo "  These are your only local copies. Backups on USB / the SSH host are NOT touched."
    if [ "${ASSUME_YES}" -eq 0 ]; then
        read -r -p "  Type DELETE to remove them, anything else to keep: " confirm
        if [ "${confirm}" = "DELETE" ]; then
            for s in "${SNAP_DIR}"/{root,home}_* "${SNAP_DIR}"/{root,home}_parent; do
                [ -e "$s" ] || continue
                btrfs subvolume delete "$s" 2>/dev/null || rm -rf "$s"
            done
            rm -f "${SNAP_DIR}/.backup-last.log"
            echo "    removed local snapshots"
        else
            echo "    kept local snapshots"
        fi
    fi
fi

echo
echo "=== Done ==="
LEFT="$(find / -xdev -name '*btrfs-restore*' 2>/dev/null || true)"
if [ -n "${LEFT}" ]; then
    echo "still on disk (expected: your kept config / snapshots):"
    echo "${LEFT}" | sed 's/^/  /'
else
    echo "no 'btrfs-restore' paths left on the system."
fi
