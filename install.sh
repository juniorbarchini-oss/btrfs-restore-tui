#!/usr/bin/env bash
# ==============================================================================
# System installer for Btrfs Restore TUI (AGY Time Machine)
# Deploys to /opt/btrfs-restore-tui with global commands in /usr/local/bin,
# and builds a self-contained Python venv there so the app never depends on
# system-wide site-packages. If the venv can't be built (no network, no venv
# module) the bin/ shims still fall back to the system python.
# ==============================================================================
set -euo pipefail

APP_NAME="btrfs-restore-tui"
INSTALL_DIR="/opt/${APP_NAME}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL_USER="${SUDO_USER:-$(id -un)}"
REAL_HOME="$(getent passwd "${REAL_USER}" | cut -d: -f6)"
DESKTOP_ENTRY="/usr/share/applications/btrfs-restore.desktop"

# --upgrade / --force accepted for forward-compat; install.sh is already
# idempotent and never touches snapshots or an existing config.
FORCE=0
for a in "$@"; do case "$a" in --upgrade|--force) FORCE=1 ;; *) echo "unknown option: $a"; exit 2 ;; esac; done

if [ "$(id -u)" -ne 0 ]; then
    echo "[!] needs root - re-running with sudo"
    exec sudo "$0" "$@"
fi

echo "=== Installing ${APP_NAME} ==="

# Retire the loose pre-app backup-now script (AGY, ~/.local/bin, unversioned):
# archive a copy next to the user config, then take it off PATH so the app's
# own command is the only backup-now.
LEGACY="${REAL_HOME}/.local/bin/backup-now"
if [ -f "${LEGACY}" ] && ! grep -q "btrfs_restore.cli_backup" "${LEGACY}" 2>/dev/null; then
    ARCHIVE="${REAL_HOME}/.config/btrfs-restore/legacy-backup-now.sh.bak"
    echo "--> retiring legacy ${LEGACY}"
    install -d -o "${REAL_USER}" -g "${REAL_USER}" "$(dirname "${ARCHIVE}")"
    install -m 600 -o "${REAL_USER}" -g "${REAL_USER}" "${LEGACY}" "${ARCHIVE}"
    rm -f "${LEGACY}"
    echo "    archived to ${ARCHIVE}"
fi

echo "--> ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"
rm -rf "${INSTALL_DIR:?}/btrfs_restore" "${INSTALL_DIR:?}/bin" "${INSTALL_DIR:?}/main.py"
cp -r "${SRC_DIR}/btrfs_restore" "${SRC_DIR}/bin" "${SRC_DIR}/main.py" \
      "${SRC_DIR}/requirements.txt" "${SRC_DIR}/config.conf.example" "${INSTALL_DIR}/"
chmod +x "${INSTALL_DIR}/main.py" "${INSTALL_DIR}/bin/"*

# --- self-contained Python environment ---------------------------------------
# The bin/ shims prefer ${INSTALL_DIR}/.venv and only fall back to the system
# python when it is absent, so a working venv here is what makes the app
# portable to a clean machine.
VENV_DIR="${INSTALL_DIR}/.venv"
if [ "${FORCE}" -eq 1 ] && [ -d "${VENV_DIR}" ]; then
    echo "--> rebuilding ${VENV_DIR}"
    rm -rf "${VENV_DIR}"
fi
if [ ! -d "${VENV_DIR}" ]; then
    if ! python3 -c 'import venv' 2>/dev/null; then
        echo "[!] python3 'venv' module missing."
        echo "    Arch:   it ships with the python package - check your install."
        echo "    Debian/Ubuntu:  sudo apt install python3-venv"
        echo "    Skipping the venv; the app will use system python packages instead."
    elif python3 -m venv "${VENV_DIR}"; then
        echo "--> ${VENV_DIR}"
        if "${VENV_DIR}/bin/pip" install --quiet --upgrade pip \
           && "${VENV_DIR}/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"; then
            echo "    installed: $(tr '\n' ' ' < "${INSTALL_DIR}/requirements.txt")"
        else
            echo "[!] could not install dependencies (offline?). Removing the half-built venv;"
            echo "    the app will fall back to system python. Re-run with --force when online."
            rm -rf "${VENV_DIR}"
        fi
    else
        echo "[!] 'python3 -m venv' failed; the app will use system python packages."
        rm -rf "${VENV_DIR}"
    fi
fi

echo "--> /usr/local/bin/{restore-tui,restore-now,backup-now}"
ln -sf "${INSTALL_DIR}/bin/restore-tui"  /usr/local/bin/restore-tui
ln -sf "${INSTALL_DIR}/bin/restore-now"  /usr/local/bin/restore-now
ln -sf "${INSTALL_DIR}/bin/backup-now"   /usr/local/bin/backup-now

# Seed the user config once (never overwrite an existing one)
USER_CFG="/home/${REAL_USER}/.config/restore-tui/config.conf"
if [ ! -f "${USER_CFG}" ] && [ ! -f "/home/${REAL_USER}/.config/btrfs-restore/config.conf" ]; then
    echo "--> seeding ${USER_CFG}"
    install -d -o "${REAL_USER}" -g "${REAL_USER}" "$(dirname "${USER_CFG}")"
    install -m 644 -o "${REAL_USER}" -g "${REAL_USER}" \
        "${SRC_DIR}/config.conf.example" "${USER_CFG}"
fi

echo "--> ${DESKTOP_ENTRY}"
cat > "${DESKTOP_ENTRY}" << 'EOF'
[Desktop Entry]
Type=Application
Name=Btrfs Restore TUI
Comment=AGY Time Machine - Btrfs backup and granular restore
Exec=foot --title="Btrfs Restore TUI" restore-tui
Icon=drive-harddisk
Terminal=false
Categories=System;Archiving;Utility;
Keywords=btrfs;backup;restore;snapshot;timemachine;
EOF
chmod 644 "${DESKTOP_ENTRY}"

chmod +x "${SRC_DIR}/uninstall.sh" 2>/dev/null || true

echo "=== Done ==="
echo "  restore-tui           menu: backup / restore / recovery"
echo "  backup-now            incremental btrfs backup to USB / SSH"
echo "  restore-now          retro TUI to browse & recover files"
echo "  restore-tui --gc      clear staging / scratch from dead runs"
echo "  restore-tui --paths   list every path the tool touches"
echo "  sudo ./uninstall.sh   remove it all (keeps your data unless --purge)"
