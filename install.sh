#!/usr/bin/env bash
# linux-privacy-switch — installer
# Usage: sudo bash install.sh [--user USERNAME] [--yes|-y]
#
# Installs everything: daemon, tray, icons, both systemd services,
# enables them, and removes legacy artifacts.
#
# --yes / -y   Non-interactive mode: skip prompts, use profile defaults,
#              skip calibration.  All steps still run with full console output.

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[OK]${NC}   $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }
info() { echo -e "${YELLOW}[>>]${NC}  $*"; }

# ── Arguments ─────────────────────────────────────────────────────────────────
NON_INTERACTIVE=false
TARGET_USER=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user)   TARGET_USER="${2:-}"; shift 2 ;;
        --yes|-y) NON_INTERACTIVE=true; shift   ;;
        *)        [[ -z "$TARGET_USER" ]] && TARGET_USER="$1"; shift ;;
    esac
done

if [[ -z "$TARGET_USER" ]]; then
    TARGET_USER=$(loginctl list-sessions --no-legend 2>/dev/null \
        | awk '{if($3!="root" && $3!="") {print $3; exit}}' || true)
    [[ -z "$TARGET_USER" ]] && TARGET_USER="${SUDO_USER:-$(logname 2>/dev/null || echo '')}"
fi

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║         linux-privacy-switch — installer                     ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
info "Session user: ${TARGET_USER:-not detected}"
echo ""

# ── Checks ────────────────────────────────────────────────────────────────────
[[ "$(id -u)" -eq 0 ]] || fail "Root required: sudo bash install.sh"
[[ -f src/privacy-switch.py ]] || fail "Run from the repository root"

# ── Dependencies ──────────────────────────────────────────────────────────────
info "Installing dependencies..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-gi gir1.2-gtk-3.0 \
    alsa-utils pipewire-bin \
    libnotify-bin 2>/dev/null || true
apt-get install -y -qq gir1.2-ayatanaappindicator3-0.1 2>/dev/null || \
    apt-get install -y -qq gir1.2-appindicator3-0.1   2>/dev/null || true
ok "Dependencies installed"

# ── Stop legacy services ──────────────────────────────────────────────────────
OLD_SVC=lenovo-privacy-switch
if systemctl is-active --quiet "$OLD_SVC" 2>/dev/null; then
    systemctl stop    "$OLD_SVC" 2>/dev/null || true
    systemctl disable "$OLD_SVC" 2>/dev/null || true
    info "Legacy service ${OLD_SVC} stopped and disabled"
fi

# ── Device detection ──────────────────────────────────────────────────────────
info "Detecting device..."
python3 src/privacy-switch.py --detect --config config/devices.conf 2>/dev/null || true

DETECTED_SECTION=""
if python3 src/privacy-switch.py \
       --config config/devices.conf \
       --detect 2>/dev/null | grep -q "^Profile:"; then
    DETECTED_SECTION=$(python3 src/privacy-switch.py \
        --detect --config config/devices.conf 2>/dev/null \
        | grep "^Profile:" | awk '{print $2}')
    ok "Device recognized in config"
else
    echo ""
    echo -e "${YELLOW}Device not found in config/devices.conf.${NC}"
    echo "For diagnostics: sudo python3 src/privacy-switch.py --monitor"
    echo ""
    if [[ "$NON_INTERACTIVE" == "true" ]]; then
        info "[AUTO] Device unknown — continuing with safe defaults"
    else
        read -rp "Continue installation anyway? [y/N] " ans
        [[ "${ans,,}" == "y" ]] || exit 1
    fi
fi

# ── Read profile defaults ─────────────────────────────────────────────────────
HW_KILL_DEFAULT=false
SYNC_CAM_DEFAULT=true
SYNC_MIC_DEFAULT=true

if [[ -n "$DETECTED_SECTION" ]]; then
    eval "$(python3 - <<PYEOF
import configparser
c = configparser.ConfigParser()
c.read("config/devices.conf")
s = "$DETECTED_SECTION"
if s in c:
    hw  = c.get(s, "has_hw_camera_kill", fallback="false").lower()
    cam = c.get(s, "sync_camera",        fallback="true").lower()
    mic = c.get(s, "sync_mic",           fallback="true").lower()
    print(f"HW_KILL_DEFAULT={hw}")
    print(f"SYNC_CAM_DEFAULT={cam}")
    print(f"SYNC_MIC_DEFAULT={mic}")
else:
    print("HW_KILL_DEFAULT=false")
    print("SYNC_CAM_DEFAULT=true")
    print("SYNC_MIC_DEFAULT=true")
PYEOF
    )"
fi

# Derive SYNC_DEFAULT (1=both, 2=camera only, 3=mic only)
if [[ "$SYNC_CAM_DEFAULT" == "true" && "$SYNC_MIC_DEFAULT" == "true" ]]; then
    SYNC_DEFAULT=1
elif [[ "$SYNC_CAM_DEFAULT" == "true" ]]; then
    SYNC_DEFAULT=2
elif [[ "$SYNC_MIC_DEFAULT" == "true" ]]; then
    SYNC_DEFAULT=3
else
    SYNC_DEFAULT=1
fi

# ── Device options ────────────────────────────────────────────────────────────
HW_KILL_ANSWER="$HW_KILL_DEFAULT"
SYNC_ANSWER="$SYNC_DEFAULT"

if [[ "$NON_INTERACTIVE" == "true" ]]; then
    info "[AUTO] has_hw_camera_kill=${HW_KILL_ANSWER}  sync_choice=${SYNC_ANSWER}"
else
    # Q1: hardware kill
    echo ""
    echo "Does your device have a PHYSICAL camera kill switch?"
    echo "(A slider that physically cuts camera sensor power — Lenovo Legion sliders do;"
    echo " Fn+key buttons typically do not.)"
    if [[ "$HW_KILL_DEFAULT" == "true" ]]; then
        read -rp "Detected profile default: YES  [Y/n]: " ans
        [[ "${ans,,}" == "n" ]] && HW_KILL_ANSWER=false || HW_KILL_ANSWER=true
    else
        read -rp "Detected profile default: NO  [y/N]: " ans
        [[ "${ans,,}" == "y" ]] && HW_KILL_ANSWER=true || HW_KILL_ANSWER=false
    fi

    # Q2: what to control
    echo ""
    echo "What should the switch control?"
    echo "  [1] Camera + Microphone"
    echo "  [2] Camera only"
    echo "  [3] Microphone only"
    read -rp "Choice [${SYNC_DEFAULT}]: " ans
    ans="${ans:-$SYNC_DEFAULT}"
    case "$ans" in
        2) SYNC_ANSWER=2 ;;
        3) SYNC_ANSWER=3 ;;
        *) SYNC_ANSWER=1 ;;
    esac
fi

# Derive final sync flags from answers
case "$SYNC_ANSWER" in
    2) SYNC_CAM_FINAL=true;  SYNC_MIC_FINAL=false ;;
    3) SYNC_CAM_FINAL=false; SYNC_MIC_FINAL=true  ;;
    *) SYNC_CAM_FINAL=true;  SYNC_MIC_FINAL=true  ;;
esac

# ── Binaries ──────────────────────────────────────────────────────────────────
info "Installing binaries..."
install -D -m 755 src/privacy-switch.py  /usr/local/bin/privacy-switch
ok "Daemon   → /usr/local/bin/privacy-switch"

install -D -m 755 src/privacy-tray.py    /usr/local/bin/privacy-tray
ok "Tray     → /usr/local/bin/privacy-tray"

install -D -m 755 src/privacy-restart.sh /usr/local/bin/privacy-restart
ok "Restart  → /usr/local/bin/privacy-restart"

# ── Icons and config ──────────────────────────────────────────────────────────
install -D -m 644 src/icons/camera-on.svg  /usr/local/share/linux-privacy-switch/icons/camera-on.svg
install -D -m 644 src/icons/camera-off.svg /usr/local/share/linux-privacy-switch/icons/camera-off.svg
ok "Icons    → /usr/local/share/linux-privacy-switch/icons/"

install -D -m 644 config/devices.conf /etc/linux-privacy-switch/devices.conf
ok "Config   → /etc/linux-privacy-switch/devices.conf"

# Patch installed config with the user's choices
if [[ -n "$DETECTED_SECTION" ]]; then
    python3 - <<PYEOF
path    = "/etc/linux-privacy-switch/devices.conf"
section = "$DETECTED_SECTION"
fields  = {
    "has_hw_camera_kill": "$HW_KILL_ANSWER",
    "sync_camera":        "$SYNC_CAM_FINAL",
    "sync_mic":           "$SYNC_MIC_FINAL",
}

with open(path) as f:
    lines = f.readlines()

in_section  = False
section_end = len(lines)
updated: set = set()

for i, line in enumerate(lines):
    stripped = line.strip()
    if stripped == f"[{section}]":
        in_section = True
        continue
    if in_section:
        if stripped.startswith("[") and stripped.endswith("]"):
            section_end = i
            in_section  = False
            break
        for key in list(fields):
            if key not in updated:
                key_part = stripped.split("=", 1)[0].strip()
                if key_part == key:
                    lines[i] = f"{key:<23} = {fields[key]}\n"
                    updated.add(key)

for key, val in fields.items():
    if key not in updated:
        lines.insert(section_end, f"{key:<23} = {val}\n")
        section_end += 1

with open(path, "w") as f:
    f.writelines(lines)
PYEOF
    ok "Device options written to /etc/linux-privacy-switch/devices.conf"
fi

mkdir -p /var/lib/linux-privacy-switch
chmod 755 /var/lib/linux-privacy-switch
ok "State    → /var/lib/linux-privacy-switch/"

# ── Systemd system service (daemon) ───────────────────────────────────────────
info "Installing system service..."
install -D -m 644 systemd/linux-privacy-switch.service \
    /etc/systemd/system/linux-privacy-switch.service

# Inject user for desktop notifications
if [[ -n "$TARGET_USER" ]]; then
    sed -i "s|privacy-switch --config|privacy-switch --user ${TARGET_USER} --config|" \
        /etc/systemd/system/linux-privacy-switch.service
    ok "Notification user: ${TARGET_USER}"
fi

systemctl daemon-reload
systemctl enable linux-privacy-switch
systemctl restart linux-privacy-switch
ok "linux-privacy-switch restarted and enabled"

# ── Systemd user service (tray) ───────────────────────────────────────────────
info "Installing tray user service..."
install -D -m 644 systemd/linux-privacy-tray.service \
    /etc/systemd/user/linux-privacy-tray.service
ok "Unit     → /etc/systemd/user/linux-privacy-tray.service"

if [[ -n "$TARGET_USER" ]]; then
    UID_TARGET=$(id -u "$TARGET_USER")
    XDG_RT="/run/user/${UID_TARGET}"
    DBUS_ADDR="unix:path=${XDG_RT}/bus"

    # Remove legacy autostart .desktop
    USER_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
    OLD_DESKTOP="$USER_HOME/.config/autostart/linux-privacy-tray.desktop"
    if [[ -f "$OLD_DESKTOP" ]]; then
        rm -f "$OLD_DESKTOP"
        info "Removed legacy autostart .desktop"
    fi

    # Enable user service (creates autostart symlink for login)
    sudo -u "$TARGET_USER" \
        XDG_RUNTIME_DIR="$XDG_RT" \
        DBUS_SESSION_BUS_ADDRESS="$DBUS_ADDR" \
        systemctl --user daemon-reload 2>/dev/null || true

    if sudo -u "$TARGET_USER" \
        XDG_RUNTIME_DIR="$XDG_RT" \
        DBUS_SESSION_BUS_ADDRESS="$DBUS_ADDR" \
        systemctl --user enable linux-privacy-tray 2>/dev/null; then
        ok "linux-privacy-tray enabled for user autostart"
    else
        info "enable failed — tray will start on next login"
    fi

    # Restart tray right now
    pkill -u "$TARGET_USER" -f privacy-tray 2>/dev/null || true
    sleep 1

    if sudo -u "$TARGET_USER" \
        XDG_RUNTIME_DIR="$XDG_RT" \
        DBUS_SESSION_BUS_ADDRESS="$DBUS_ADDR" \
        systemctl --user restart linux-privacy-tray 2>/dev/null; then
        ok "linux-privacy-tray restarted via systemd --user"
    else
        # Fallback: direct launch if graphical-session.target is not yet active
        X_SOCK=$(ls /tmp/.X11-unix/ 2>/dev/null | sort | head -1 || true)
        DISPLAY_VAL=":${X_SOCK#X}"
        sudo -u "$TARGET_USER" \
            DISPLAY="$DISPLAY_VAL" \
            DBUS_SESSION_BUS_ADDRESS="$DBUS_ADDR" \
            XDG_RUNTIME_DIR="$XDG_RT" \
            /usr/local/bin/privacy-tray </dev/null &>/dev/null &
        ok "Tray launched directly (fallback, DISPLAY=${DISPLAY_VAL})"
    fi
fi

# ── Optional calibration ──────────────────────────────────────────────────────
if [[ "$NON_INTERACTIVE" != "true" && "$HW_KILL_ANSWER" == "true" ]]; then
    echo ""
    read -rp "Run threshold calibration now? (Recommended for first install) [Y/n]: " ans
    if [[ "${ans,,}" != "n" ]]; then
        python3 /usr/local/bin/privacy-switch --calibrate \
            --config /etc/linux-privacy-switch/devices.conf || true
    fi
fi

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Installation complete                                       ║"
echo "║                                                              ║"
echo "║  Verify:                                                     ║"
echo "║    sudo systemctl status linux-privacy-switch                ║"
echo "║    systemctl --user status linux-privacy-tray                ║"
echo "║    bash tests/verify.sh                                      ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
systemctl status linux-privacy-switch --no-pager -l | head -15
echo ""
if [[ -n "$TARGET_USER" ]]; then
    sudo -u "$TARGET_USER" \
        XDG_RUNTIME_DIR="/run/user/$(id -u "$TARGET_USER")" \
        systemctl --user status linux-privacy-tray --no-pager 2>/dev/null | head -10 || true
fi
