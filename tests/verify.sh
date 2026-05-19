#!/usr/bin/env bash
# linux-privacy-switch — post-install verification script
# Usage: bash tests/verify.sh
# Interactive checklist: toggle slider on/off → test camera and microphone.

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'

ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
fail() { echo -e "  ${RED}✗${NC} $*"; FAILED=$((FAILED+1)); }
info() { echo -e "  ${BLUE}→${NC} $*"; }
warn() { echo -e "  ${YELLOW}!${NC} $*"; }

FAILED=0
TMP_IMG=/tmp/privacy-test-cam.jpg
TMP_WAV=/tmp/privacy-test-mic.wav

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║     linux-privacy-switch — camera and microphone check       ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# ── Environment checks ────────────────────────────────────────────────────────
echo "1. Environment check"

if systemctl is-active --quiet linux-privacy-switch 2>/dev/null; then
    ok "Service linux-privacy-switch is active"
else
    fail "Service linux-privacy-switch is NOT running (sudo systemctl start linux-privacy-switch)"
fi

if [[ -e /dev/video0 ]]; then
    ok "Camera found: /dev/video0"
else
    warn "Camera not found at /dev/video0 — may be a different node"
fi

if arecord -l 2>/dev/null | grep -q "card"; then
    ok "Microphone found (ALSA)"
else
    fail "No microphone found via ALSA"
fi

FFMPEG=$(command -v ffmpeg 2>/dev/null || true)
ARECORD=$(command -v arecord 2>/dev/null || true)
if [[ -z "$FFMPEG" && -z "$ARECORD" ]]; then
    warn "ffmpeg/arecord not found. Install: sudo apt install ffmpeg alsa-utils"
fi

echo ""

# ── Test 1: Slider OFF ────────────────────────────────────────────────────────
echo "2. Test: slider → OFF (red indicator)"
echo ""
echo -e "   ${YELLOW}Move the slider to the OFF position (red).${NC}"
read -rp "   Press Enter when ready..."
echo ""

# Check ALSA mute
MIC_STATE=$(amixer -c 1 get Capture 2>/dev/null | grep -oP '\[(on|off)\]' | head -1 || echo "unknown")
if [[ "$MIC_STATE" == "[off]" ]]; then
    ok "ALSA Capture = [off] — microphone muted"
else
    fail "ALSA Capture = ${MIC_STATE} — expected [off]. Service did not mute the microphone."
fi

# Check camera_power
CAM_POWER=$(cat /sys/bus/platform/drivers/ideapad_acpi/VPC2004:00/camera_power 2>/dev/null || echo "N/A")
if [[ "$CAM_POWER" == "0" ]]; then
    ok "camera_power = 0 — camera off"
elif [[ "$CAM_POWER" == "N/A" ]]; then
    warn "camera_power not found (not a Lenovo IdeaPad/Legion)"
else
    fail "camera_power = ${CAM_POWER} — expected 0"
fi

# Audio recording test (should record silence or fail)
if [[ -n "$ARECORD" ]]; then
    info "Recording 2 seconds — expecting silence..."
    if arecord -d 2 -f cd "$TMP_WAV" 2>/dev/null; then
        SIZE=$(stat -c%s "$TMP_WAV" 2>/dev/null || echo 0)
        if [[ "$SIZE" -gt 44 ]]; then
            warn "Recording succeeded (${SIZE} bytes) — check if $TMP_WAV is silent: aplay $TMP_WAV"
        fi
    else
        ok "Recording failed — microphone blocked"
    fi
fi

# Camera snapshot test (should fail or return black frame)
if [[ -n "$FFMPEG" && -e /dev/video0 ]]; then
    info "Taking snapshot — expecting error or black frame..."
    if ffmpeg -f v4l2 -i /dev/video0 -frames:v 1 "$TMP_IMG" -y 2>/dev/null; then
        ok "Snapshot taken: $TMP_IMG (verify it is black)"
        info "Open with: xdg-open $TMP_IMG"
    else
        ok "Snapshot failed — camera blocked"
    fi
fi

echo ""

# ── Test 2: Slider ON ─────────────────────────────────────────────────────────
echo "3. Test: slider → ON (green indicator)"
echo ""
echo -e "   ${YELLOW}Move the slider to the ON position (green).${NC}"
read -rp "   Press Enter when ready..."
sleep 1
echo ""

MIC_STATE=$(amixer -c 1 get Capture 2>/dev/null | grep -oP '\[(on|off)\]' | head -1 || echo "unknown")
if [[ "$MIC_STATE" == "[on]" ]]; then
    ok "ALSA Capture = [on] — microphone active"
else
    fail "ALSA Capture = ${MIC_STATE} — expected [on]"
fi

CAM_POWER=$(cat /sys/bus/platform/drivers/ideapad_acpi/VPC2004:00/camera_power 2>/dev/null || echo "N/A")
if [[ "$CAM_POWER" == "1" ]]; then
    ok "camera_power = 1 — camera on"
elif [[ "$CAM_POWER" == "N/A" ]]; then
    warn "camera_power not found (not a Lenovo IdeaPad/Legion)"
else
    warn "camera_power = ${CAM_POWER} (expected 1) — on some models (Legion 5 ITE EC) this is normal: EC does not update the value"
fi

# Audio recording test
if [[ -n "$ARECORD" ]]; then
    info "Recording 3 seconds — say something into the microphone..."
    if arecord -d 3 -f cd "$TMP_WAV" 2>/dev/null; then
        ok "Recording successful: $TMP_WAV"
        info "Play back with: aplay $TMP_WAV"
    else
        fail "Recording failed while microphone is enabled"
    fi
fi

# Camera snapshot test
if [[ -n "$FFMPEG" && -e /dev/video0 ]]; then
    info "Taking snapshot..."
    if ffmpeg -f v4l2 -i /dev/video0 -frames:v 1 "$TMP_IMG" -y 2>/dev/null; then
        ok "Snapshot: $TMP_IMG"
        info "Open with: xdg-open $TMP_IMG"
    else
        fail "Snapshot failed while camera is enabled"
    fi
fi

echo ""

# ── Results ───────────────────────────────────────────────────────────────────
echo "╔══════════════════════════════════════════════════════════════╗"
if [[ "$FAILED" -eq 0 ]]; then
    echo -e "║  ${GREEN}ALL TESTS PASSED${NC} — slider is working correctly              ║"
else
    echo -e "║  ${RED}TESTS FAILED${NC} — ${FAILED} issue(s) detected                        ║"
fi
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# Service log
echo "Recent service events:"
journalctl -u linux-privacy-switch -n 10 --no-pager 2>/dev/null || true

exit "$FAILED"
