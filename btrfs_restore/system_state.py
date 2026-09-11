"""
Captures the parts of an Arch / Omarchy system that live outside $HOME and are
needed to rebuild the machine from bare metal: OS identity, the explicit and AUR
package lists, pacman config & mirrors, enabled systemd units, Flatpaks, keyd,
custom /usr/local/bin scripts, a few /etc files, the bootloader config and the
user crontab.

Also writes `restore.sh`: a self-contained bash recovery script that replays all
of the above plus the home tree, with zero dependency on this app or Python.

Everything here is best-effort and read-only against the live system. The sibling
ext4 edition has the Debian variant of this; the interface is identical so the
two can merge behind one `system_state/` package with `arch.py` / `debian.py`.
"""
import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List

# Hardware / kernel packages - reinstalling them on a different machine is
# pointless or harmful, so they are dropped from the explicit list.
_DRIVER_RE = [
    re.compile(p, re.IGNORECASE) for p in (
        r"^nvidia(-.*)?$", r"^lib32-nvidia.*", r"^nvidia-utils$", r"^opencl-nvidia$",
        r"^linux(-.*)?$", r"^linux-firmware.*", r"^amd-ucode$", r"^intel-ucode$",
        r"^broadcom-wl.*", r"^b43-.*", r"^mesa$", r"^vulkan-radeon$", r"^vulkan-intel$",
    )
]


class SystemStateCollector:
    def __init__(self, snapshot_dir: Path):
        self.snapshot_dir = snapshot_dir
        self.meta_dir = snapshot_dir / "_system_state"
        self.warnings: List[str] = []

    # -- public ------------------------------------------------------

    def collect_all(self) -> List[str]:
        self.warnings = []
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        for step in (
            self._export_os_info,
            self._export_packages,
            self._export_pacman_config,
            self._export_systemd,
            self._export_flatpaks,
            self._export_omarchy,
            self._export_bootloader,
            self._export_etc_configs,
            self._export_usr_local_bin,
            self._export_keyd,
            self._export_crontab,
            self._generate_restore_script,
        ):
            try:
                step()
            except Exception as exc:  # noqa: BLE001 - best-effort by design
                self.warnings.append(f"{getattr(step, '__name__', 'step')}: {exc}")
        return self.warnings

    # -- helpers ---------------------------------------------------

    def _run(self, cmd: List[str], timeout: int = 60) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def _write(self, name: str, content: str) -> None:
        (self.meta_dir / name).write_text(content)

    # directory names never worth copying (regenerated, huge, or secret)
    _COPY_SKIP = {"gnupg", "__pycache__", ".git"}

    def _copy(self, src: str, dst_subdir: str) -> None:
        p = Path(src)
        if not p.exists():
            return
        target = self.meta_dir / dst_subdir
        target.mkdir(parents=True, exist_ok=True)
        if p.is_dir():
            for item in p.rglob("*"):
                if any(part in self._COPY_SKIP for part in item.parts):
                    continue
                if item.is_file() and not item.is_symlink():
                    try:
                        rel = item.relative_to(p)
                        (target / rel).parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, target / rel)
                    except OSError as exc:
                        self.warnings.append(f"copy {item}: {exc}")
        else:
            try:
                shutil.copy2(p, target / p.name)
            except OSError as exc:
                self.warnings.append(f"copy {src}: {exc}")

    # -- exporters -------------------------------------------------

    def _export_os_info(self):
        info = {
            "hostname": os.uname().nodename,
            "arch": os.uname().machine,
            "kernel": os.uname().release,
            "user": os.getenv("SUDO_USER") or os.getenv("USER", "unknown"),
            "desktop": os.environ.get("XDG_CURRENT_DESKTOP", ""),
            "session": os.environ.get("XDG_SESSION_TYPE", ""),
            "captured_at": datetime.now().isoformat(),
        }
        osr = Path("/etc/os-release")
        if osr.is_file():
            for line in osr.read_text().splitlines():
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip().strip('"')
                if k == "NAME":
                    info["distro"] = v
                elif k == "BUILD_ID":
                    info["build_id"] = v
        self._write("os_info.json", json.dumps(info, indent=2))

    def _export_packages(self):
        if not shutil.which("pacman"):
            self.warnings.append("pacman not found; package lists not saved")
            return
        res_m = self._run(["pacman", "-Qqm"])
        aur = set(res_m.stdout.split()) if res_m.returncode == 0 else set()
        if res_m.returncode == 0:
            self._write("pkglist_aur.txt", res_m.stdout)
        res = self._run(["pacman", "-Qqe"])
        if res.returncode == 0:
            # `-Qqe` includes AUR/foreign packages too (they're also "explicit") -
            # keep this list repo-only so restore.sh's plain `pacman -S` never
            # hits "target not found" on a name only AUR knows; pkglist_aur.txt
            # already covers those, via the AUR helper.
            pkgs = sorted(p for line in res.stdout.splitlines()
                          if (p := line.strip()) and p not in aur
                          and not any(rx.match(p) for rx in _DRIVER_RE))
            self._write("pkglist_explicit.txt", "\n".join(pkgs) + "\n")
        # full list with versions, for reference / diffing
        res_all = self._run(["pacman", "-Q"])
        if res_all.returncode == 0:
            self._write("pkglist_all_versions.txt", res_all.stdout)

    def _export_pacman_config(self):
        self._copy("/etc/pacman.conf", "pacman")
        self._copy("/etc/pacman.d", "pacman/pacman.d")

    def _export_systemd(self):
        r = self._run(["systemctl", "list-unit-files", "--state=enabled",
                       "--no-legend", "--no-pager"])
        if r.returncode == 0:
            self._write("systemd_enabled.txt", r.stdout)
        user = os.getenv("SUDO_USER")
        if user:
            ru = self._run(["runuser", "-u", user, "--", "systemctl", "--user",
                            "list-unit-files", "--state=enabled", "--no-legend", "--no-pager"])
            if ru.returncode == 0 and ru.stdout.strip():
                self._write("systemd_enabled_user.txt", ru.stdout)

    def _export_flatpaks(self):
        if shutil.which("flatpak"):
            r = self._run(["flatpak", "list", "--app", "--columns=application"])
            if r.returncode == 0:
                self._write("flatpak_list.txt", r.stdout)

    def _export_omarchy(self):
        info = {}
        ver = Path("/usr/share/omarchy/version") if Path("/usr/share/omarchy").exists() else None
        if ver and ver.is_file():
            info["omarchy_version"] = ver.read_text().strip()
        if shutil.which("omarchy-version"):
            r = self._run(["omarchy-version"])
            if r.returncode == 0:
                info["omarchy_version"] = r.stdout.strip()
        info["note"] = "omarchy user config lives under $HOME/.config and is in the home snapshot"
        self._write("omarchy.json", json.dumps(info, indent=2))

    def _export_bootloader(self):
        found = {}
        for name, paths in {
            "limine": ["/boot/limine.conf", "/etc/limine.conf", "/boot/EFI/limine"],
            "grub": ["/etc/default/grub", "/boot/grub/grub.cfg"],
            "systemd-boot": ["/boot/loader/loader.conf", "/boot/loader/entries"],
        }.items():
            for p in paths:
                if Path(p).exists():
                    found.setdefault(name, []).append(p)
                    self._copy(p, f"bootloader/{name}")
        self._write("bootloader.json", json.dumps(found, indent=2))

    def _export_etc_configs(self):
        for f in ("/etc/hosts", "/etc/hostname", "/etc/fstab", "/etc/environment",
                  "/etc/locale.conf", "/etc/vconsole.conf", "/etc/mkinitcpio.conf",
                  "/etc/systemd/timesyncd.conf", "/etc/systemd/logind.conf"):
            self._copy(f, "etc")

    def _export_usr_local_bin(self):
        ulb = Path("/usr/local/bin")
        if not ulb.is_dir():
            return
        target = self.meta_dir / "usr_local_bin"
        target.mkdir(parents=True, exist_ok=True)
        for item in ulb.iterdir():
            if item.is_file() and not item.is_symlink():
                try:
                    if item.stat().st_size <= 15 * 1024 * 1024:
                        shutil.copy2(item, target / item.name)
                except OSError as exc:
                    self.warnings.append(f"/usr/local/bin/{item.name}: {exc}")

    def _export_keyd(self):
        self._copy("/etc/keyd", "keyd")

    def _export_crontab(self):
        user = os.getenv("SUDO_USER")
        cmd = ["crontab", "-l"] if not user else ["runuser", "-u", user, "--", "crontab", "-l"]
        if shutil.which("crontab"):
            r = self._run(cmd)
            if r.returncode == 0 and r.stdout.strip():
                self._write("crontab.txt", r.stdout)

    # -- bare-metal restore script -------------------------------

    def _generate_restore_script(self):
        script = self.snapshot_dir / "restore.sh"
        script.write_text(_RESTORE_SH)
        script.chmod(0o755)


_RESTORE_SH = r"""#!/usr/bin/env bash
# ==============================================================================
# btrfs-restore-tui - Bare-Metal Disaster Recovery (Arch / Omarchy)
# Generated automatically during backup. Run from INSIDE this snapshot folder,
# on a freshly installed Arch base (booted, btrfs root, your user created), or
# from a live ISO with the new root mounted at /mnt (pass --root /mnt).
#
# Dependencies: bash, coreutils, rsync, pacman, btrfs-progs, zstd. Nothing else.
# ==============================================================================
set -euo pipefail

GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; RED=$'\033[0;31m'; BLUE=$'\033[0;34m'; NC=$'\033[0m'
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${SCRIPT_DIR}/_system_state"
ROOT_PREFIX=""
DO_HOME=1; DO_PKGS=1; DO_ETC=1

while [ $# -gt 0 ]; do
    case "$1" in
        --root) ROOT_PREFIX="${2%/}"; shift 2 ;;
        --no-home) DO_HOME=0; shift ;;
        --no-packages) DO_PKGS=0; shift ;;
        --no-etc) DO_ETC=0; shift ;;
        -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1"; exit 2 ;;
    esac
done

# The home-tree step (rsync of root-owned files, chown -R) needs root. When we
# are restoring the booted system (no --root) and were not started with sudo,
# re-exec under sudo so [5/6] doesn't die on "Operation not permitted".
if [ -z "${ROOT_PREFIX}" ] && [ "$(id -u)" -ne 0 ]; then
    echo -e "${YELLOW}This recovery needs root - re-running with sudo...${NC}"
    exec sudo -E "$0" "$@"
fi

TARGET_USER="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["user"])' "${STATE_DIR}/os_info.json" 2>/dev/null || echo "${SUDO_USER:-$USER}")"
TARGET_HOME="${ROOT_PREFIX}/home/${TARGET_USER}"

# home_<ts> is either a received btrfs subvolume or a .btrfs.zst stream
HOME_SRC=""
for d in "${SCRIPT_DIR}"/home_*; do [ -d "$d" ] && HOME_SRC="$d"; done
HOME_STREAM=""; [ -f "${SCRIPT_DIR}/home.btrfs.zst" ] && HOME_STREAM="${SCRIPT_DIR}/home.btrfs.zst"

echo -e "${GREEN}=================================================================${NC}"
echo -e "${GREEN}   btrfs-restore-tui - BARE-METAL RECOVERY (Arch / Omarchy)       ${NC}"
echo -e "${GREEN}=================================================================${NC}"
echo    "Snapshot : ${SCRIPT_DIR}"
echo    "User     : ${TARGET_USER}"
echo    "Home     : ${TARGET_HOME}"
[ -n "${ROOT_PREFIX}" ] && echo "Root pfx : ${ROOT_PREFIX}"
echo ""
read -r -p "Proceed with recovery from this snapshot? (yes/no): " C
[ "${C}" = "yes" ] || { echo -e "${YELLOW}Aborted.${NC}"; exit 0; }

run() { if [ -n "${ROOT_PREFIX}" ]; then arch-chroot "${ROOT_PREFIX}" "$@"; else sudo "$@"; fi; }

if [ "${DO_PKGS}" = 1 ] && command -v pacman >/dev/null; then
    echo -e "${BLUE}[1/6] pacman config & mirrors${NC}"
    if [ -d "${STATE_DIR}/pacman" ]; then
        [ -f "${STATE_DIR}/pacman/pacman.conf" ] && run cp "${STATE_DIR}/pacman/pacman.conf" /etc/pacman.conf || true
        [ -d "${STATE_DIR}/pacman/pacman.d" ] && run cp -rn "${STATE_DIR}/pacman/pacman.d/." /etc/pacman.d/ || true
    fi
    run pacman -Sy --noconfirm archlinux-keyring || true
    # Omarchy's 00-omarchy-update-guard.hook refuses a direct `pacman -Syu`
    # (it wants `omarchy update` instead) - opt back in for this one call, a
    # real recovery, not a casual upgrade. No-op on plain Arch.
    OMARCHY_BYPASS=()
    if command -v omarchy >/dev/null 2>&1 || grep -qi '^NAME="\?Omarchy' /etc/os-release 2>/dev/null; then
        OMARCHY_BYPASS=(env OMARCHY_ALLOW_DIRECT_PACMAN=1)
    fi
    run "${OMARCHY_BYPASS[@]}" pacman -Syu --noconfirm || true

    echo -e "${BLUE}[2/6] explicit packages${NC}"
    if [ -f "${STATE_DIR}/pkglist_explicit.txt" ]; then
        mapfile -t PKGS < "${STATE_DIR}/pkglist_explicit.txt"
        run pacman -S --needed --noconfirm "${PKGS[@]}" || \
            echo -e "${YELLOW}  some packages failed; re-run manually with --needed${NC}"
    fi

    echo -e "${BLUE}[3/6] AUR packages${NC}"
    if [ -f "${STATE_DIR}/pkglist_aur.txt" ] && [ -s "${STATE_DIR}/pkglist_aur.txt" ]; then
        AUR_HELPER=""
        AUR_EXTRA=()
        for h in yay paru; do command -v "$h" >/dev/null && AUR_HELPER="$h" && break; done
        # Never let the helper reach for a tty (edits/diffs/cleanup prompts) -
        # a recovery run may have none. yay and paru spell "don't ask" differently.
        case "${AUR_HELPER}" in
            yay)  AUR_EXTRA=(--answerclean None --answerdiff None --answeredit None --answerupgrade None) ;;
            paru) AUR_EXTRA=(--skipreview) ;;
        esac
        if [ -n "${AUR_HELPER}" ]; then
            sudo -u "${TARGET_USER}" "${AUR_HELPER}" -S --needed --noconfirm "${AUR_EXTRA[@]}" \
                - < "${STATE_DIR}/pkglist_aur.txt" || {
                echo -e "${YELLOW}  AUR install failed (some builds need a real terminal) - retry manually:${NC}"
                echo    "  ${AUR_HELPER} -S --needed - < ${STATE_DIR}/pkglist_aur.txt"
            }
        else
            echo -e "${YELLOW}  no AUR helper (yay/paru) - install one, then:${NC}"
            echo    "  yay -S --needed - < ${STATE_DIR}/pkglist_aur.txt"
        fi
    fi
else
    echo -e "${YELLOW}[1-3/6] packages skipped${NC}"
fi

echo -e "${BLUE}[4/6] Flatpaks${NC}"
if [ -f "${STATE_DIR}/flatpak_list.txt" ] && command -v flatpak >/dev/null; then
    while IFS= read -r app; do [ -n "$app" ] && flatpak install -y flathub "$app" 2>/dev/null || true; done \
        < "${STATE_DIR}/flatpak_list.txt"
fi

if [ "${DO_HOME}" = 1 ]; then
    echo -e "${BLUE}[5/6] home tree -> ${TARGET_HOME}${NC}"
    mkdir -p "${TARGET_HOME}"
    if [ -n "${HOME_SRC}" ] && [ -d "${HOME_SRC}/${TARGET_USER}" ]; then
        rsync -aAX --numeric-ids --info=progress2 "${HOME_SRC}/${TARGET_USER}/" "${TARGET_HOME}/"
    elif [ -n "${HOME_SRC}" ]; then
        rsync -aAX --numeric-ids --info=progress2 "${HOME_SRC}/" "${TARGET_HOME}/"
    elif [ -n "${HOME_STREAM}" ]; then
        STAGE="$(mktemp -d)"; echo "  receiving stream into ${STAGE}"
        zstd -dc "${HOME_STREAM}" | sudo btrfs receive "${STAGE}"
        SUB="$(find "${STAGE}" -maxdepth 1 -mindepth 1 -type d | head -1)"
        rsync -aAX --numeric-ids --info=progress2 "${SUB}/${TARGET_USER:-}/" "${TARGET_HOME}/" 2>/dev/null \
            || rsync -aAX --numeric-ids --info=progress2 "${SUB}/" "${TARGET_HOME}/"
        sudo btrfs subvolume delete "${SUB}" 2>/dev/null || true; rm -rf "${STAGE}"
    else
        echo -e "${YELLOW}  no home data in this snapshot${NC}"
    fi
    if id "${TARGET_USER}" >/dev/null 2>&1; then
        chown -R "${TARGET_USER}:${TARGET_USER}" "${TARGET_HOME}" || true
    fi
fi

echo -e "${BLUE}[6/6] system config${NC}"
if [ "${DO_ETC}" = 1 ]; then
    [ -d "${STATE_DIR}/etc" ] && run cp -n "${STATE_DIR}/etc/." "${ROOT_PREFIX}/etc/" 2>/dev/null || true
    [ -d "${STATE_DIR}/keyd" ] && { run mkdir -p /etc/keyd; run cp -n "${STATE_DIR}/keyd/." /etc/keyd/ 2>/dev/null || true; }
    if [ -d "${STATE_DIR}/usr_local_bin" ]; then
        run mkdir -p /usr/local/bin
        run cp -n "${STATE_DIR}/usr_local_bin/." /usr/local/bin/ 2>/dev/null || true
    fi
    if [ -f "${STATE_DIR}/systemd_enabled.txt" ]; then
        awk '{print $1}' "${STATE_DIR}/systemd_enabled.txt" | while read -r u; do
            [ -n "$u" ] && run systemctl enable "$u" 2>/dev/null || true
        done
    fi
    [ -f "${STATE_DIR}/crontab.txt" ] && sudo -u "${TARGET_USER}" crontab "${STATE_DIR}/crontab.txt" || true
fi

echo -e "${GREEN}=================================================================${NC}"
echo -e "${GREEN}  RECOVERY COMPLETE${NC}"
echo -e "  - review /etc/fstab and the bootloader config under ${STATE_DIR}/bootloader/"
echo -e "  - regenerate initramfs if needed:  sudo mkinitcpio -P"
echo -e "  - reboot"
echo -e "${GREEN}=================================================================${NC}"
"""
