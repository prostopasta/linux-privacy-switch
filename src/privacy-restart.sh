#!/bin/bash
# Restarts daemon + tray. Waits until the daemon determines the initial state.
set -e

TARGET_USER="${1:-$(logname 2>/dev/null || echo "$SUDO_USER")}"
[[ -z "$TARGET_USER" ]] && TARGET_USER=$(loginctl list-sessions --no-legend 2>/dev/null \
    | awk '{if($3!="root" && $3!="") {print $3; exit}}')

UID_U=$(id -u "$TARGET_USER")
XDG_RT="/run/user/${UID_U}"
DBUS_ADDR="unix:path=${XDG_RT}/bus"

echo "Restarting linux-privacy-switch..."
systemctl restart linux-privacy-switch

echo "Waiting for daemon to determine initial state (~5s)..."
for _ in $(seq 1 20); do
    sleep 1
    STATE=$(cat /var/lib/linux-privacy-switch/state 2>/dev/null)
    if [[ -n "$STATE" ]]; then
        MTIME=$(stat -c %Y /var/lib/linux-privacy-switch/state 2>/dev/null || echo 0)
        NOW=$(date +%s)
        AGE=$(( NOW - MTIME ))
        if [[ $AGE -le 5 ]]; then
            echo "Daemon ready (state=$STATE)"
            break
        fi
    fi
done

echo "Restarting tray..."
sudo -u "$TARGET_USER" \
    XDG_RUNTIME_DIR="$XDG_RT" \
    DBUS_SESSION_BUS_ADDRESS="$DBUS_ADDR" \
    systemctl --user restart linux-privacy-tray 2>/dev/null || true

echo "Done. State = $(cat /var/lib/linux-privacy-switch/state 2>/dev/null)"
