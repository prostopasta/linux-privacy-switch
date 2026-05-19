#!/usr/bin/env bash
# linux-privacy-switch — installer
# Usage: sudo bash install.sh [--user USERNAME]
#
# Installs everything: daemon, tray, icons, both systemd services,
# enables them, and removes legacy artifacts.

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
ok()   { echo -e "${GREEN}[OK]${NC}   $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*"; exit 1; }
info() { echo -e "${YELLOW}[>>]${NC}  $*"; }

# ── Arguments ─────────────────────────────────────────────────────────────────
TARGET_USER="${1:-}"
if [[ "$TARGET_USER" == "--user" ]]; then
    TARGET_USER="${2:-}"
fi
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

if ! python3 src/privacy-switch.py \
       --config config/devices.conf \
       --detect 2>/dev/null | grep -q "^Profile:"; then
    echo ""
    echo -e "${YELLOW}Device not found in config/devices.conf.${NC}"
    echo "For diagnostics: sudo python3 src/privacy-switch.py --monitor"
    echo ""
    read -rp "Continue installation anyway? [y/N] " ans
    [[ "${ans,,}" == "y" ]] || exit 1
else
    ok "Device recognized in config"
fi

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
