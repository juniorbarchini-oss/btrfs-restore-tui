#!/usr/bin/env bash
# ==============================================================================
# System Installer for Btrfs Restore TUI (AGY Time Explorer)
# Complies with Official Ecosystem Rule 7 (OS Standard Deployment)
# ==============================================================================
set -euo pipefail

APP_NAME="btrfs-restore-tui"
INSTALL_DIR="/opt/${APP_NAME}"
BIN_LINK="/usr/local/bin/restore-now"
DESKTOP_ENTRY="/usr/share/applications/btrfs-restore.desktop"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Installing ${APP_NAME} to System ==="

# 1. Require root privileges to install in /opt and /usr
if [ "$(id -u)" -ne 0 ]; then
    echo "[!] This script requires administrative privileges."
    exec sudo "$0" "$@"
fi

# 2. Copy application files to /opt/btrfs-restore-tui/
echo "--> Copying files to ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"
rm -rf "${INSTALL_DIR:?}"/*

cp -r "${SRC_DIR}/main.py" "${INSTALL_DIR}/"
cp -r "${SRC_DIR}/btrfs_restore" "${INSTALL_DIR}/"
chmod +x "${INSTALL_DIR}/main.py"

# 3. Create global executable link in /usr/local/bin/restore-now
echo "--> Creating global executable at ${BIN_LINK}..."
cat << 'EOF' > "${BIN_LINK}"
#!/usr/bin/env bash
exec sudo --preserve-env=WAYLAND_DISPLAY,DISPLAY,XDG_RUNTIME_DIR,TERM,SUDO_USER,USER python3 /opt/btrfs-restore-tui/main.py "$@"
EOF
chmod +x "${BIN_LINK}"

# 4. Create desktop application entry in /usr/share/applications/btrfs-restore.desktop
echo "--> Registering desktop entry in ${DESKTOP_ENTRY}..."
cat << EOF > "${DESKTOP_ENTRY}"
[Desktop Entry]
Type=Application
Name=Btrfs Restore TUI
Comment=AGY Time Explorer - Granular Btrfs snapshot file restoration
Exec=foot --title="Btrfs Restore TUI" restore-now
Icon=drive-harddisk
Terminal=false
Categories=System;Archiving;Utility;
Keywords=btrfs;restore;backup;snapshot;timemachine;
EOF

chmod 644 "${DESKTOP_ENTRY}"

echo "=== Installation successfully completed ==="
echo "You can launch the restore tool from anywhere using: restore-now"
echo "Or search for 'Btrfs Restore TUI' in your application launcher."
