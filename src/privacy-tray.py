#!/usr/bin/env python3
"""
privacy-tray — system tray indicator for linux-privacy-switch.

Shows a green (on) or red (off) camera icon.
Runs as the desktop user via systemd user service.

Dependencies: gir1.2-ayatanaappindicator3-0.1 python3-gi
"""

import os
import subprocess
import sys
import configparser
import time

import gi
gi.require_version("Gtk", "3.0")

# Support Ayatana (Ubuntu 22.04+) and legacy AppIndicator3
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator3
    INDICATOR_MODULE = "AyatanaAppIndicator3"
except (ValueError, ImportError):
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3
    INDICATOR_MODULE = "AppIndicator3"

from gi.repository import Gtk, GLib
import logging

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [privacy-tray] %(levelname)s: %(message)s")

STATE_FILE         = "/var/lib/linux-privacy-switch/state"
HEARTBEAT_FILE     = "/var/lib/linux-privacy-switch/heartbeat"
LOCK_FILE          = f"/run/user/{os.getuid()}/privacy-tray.lock"
ICON_DIR           = "/usr/local/share/linux-privacy-switch/icons"
ICON_ON            = "camera-on"
ICON_OFF           = "camera-off"
POLL_INTERVAL_MS   = 800
HEARTBEAT_MAX_AGE  = 30  # seconds; if older than this — daemon is dead


def _load_alsa_config():
    cfg = configparser.ConfigParser()
    cfg.read("/etc/linux-privacy-switch/devices.conf")
    for section in cfg.sections():
        card = cfg.get(section, "alsa_card", fallback=None)
        control = cfg.get(section, "alsa_control", fallback=None)
        if card and control:
            return card, control
    return "1", "Capture Switch"


ALSA_CARD, ALSA_CONTROL = _load_alsa_config()


def mic_apply(enabled: bool):
    """ALSA (hardware capture) + PipeWire (wpctl). Tray starts after PipeWire."""
    alsa_val = "cap" if enabled else "nocap"
    mute     = "0"   if enabled else "1"

    try:
        subprocess.run(
            ["amixer", "-c", ALSA_CARD, "sset", ALSA_CONTROL, alsa_val],
            capture_output=True, timeout=3,
        )
    except Exception:
        pass

    try:
        r = subprocess.run(
            ["wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", mute],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            logging.warning("wpctl set-mute: %s", r.stderr.strip())
        else:
            logging.info("wpctl mic → %s", "unmuted" if enabled else "muted")
    except Exception as e:
        logging.warning("wpctl set-mute: %s", e)

    if enabled:
        # Set capture volume to 50% on enable — 100% is too loud for speech recognition.
        try:
            subprocess.run(
                ["wpctl", "set-volume", "@DEFAULT_AUDIO_SOURCE@", "0.50"],
                capture_output=True, timeout=3,
            )
        except Exception:
            pass


def read_state() -> bool | None:
    """True=on, False=off, None=file not found."""
    try:
        with open(STATE_FILE) as f:
            content = f.read().strip()
        if ":" in content:
            return content.split(":", 1)[1] == "1"
        return content == "1"
    except Exception:
        return None


def daemon_alive() -> bool:
    """True if the daemon is alive (heartbeat fresher than HEARTBEAT_MAX_AGE seconds)."""
    try:
        with open(HEARTBEAT_FILE) as f:
            ts = int(f.read().strip())
        return (int(time.time()) - ts) < HEARTBEAT_MAX_AGE
    except Exception:
        return False



class PrivacyTray:
    def __init__(self):
        self.current_state: bool | None = None

        self.indicator = AppIndicator3.Indicator.new(
            "linux-privacy-switch",
            ICON_OFF,
            AppIndicator3.IndicatorCategory.HARDWARE,
        )
        # Set custom icon directory before set_icon_full
        self.indicator.set_icon_theme_path(ICON_DIR)
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)

        self.menu = Gtk.Menu()

        self.label_item = Gtk.MenuItem(label="Status: unknown")
        self.label_item.set_sensitive(False)
        self.menu.append(self.label_item)

        self.menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", self._on_quit)
        self.menu.append(quit_item)

        self.menu.show_all()
        self.indicator.set_menu(self.menu)

        self._update_icon()

        GLib.timeout_add(POLL_INTERVAL_MS, self._poll)

    def _poll(self) -> bool:
        self._update_icon()
        return True

    def _update_icon(self):
        if not daemon_alive():
            if self.current_state != "dead":
                self.current_state = "dead"
                self.indicator.set_icon_full(ICON_OFF, "Daemon not responding")
                self.label_item.set_label("⚠️ Daemon not responding")
            return

        state = read_state()
        if state == self.current_state:
            return
        self.current_state = state

        if state is True:
            self.indicator.set_icon_full(ICON_ON, "Camera and microphone: ON")
            self.label_item.set_label("📷🎤 Camera and microphone: ON")
            mic_apply(True)
        elif state is False:
            self.indicator.set_icon_full(ICON_OFF, "Camera and microphone: OFF")
            self.label_item.set_label("🚫🔇 Camera and microphone: OFF")
            mic_apply(False)
        else:
            self.indicator.set_icon_full(ICON_OFF, "Starting...")
            self.label_item.set_label("⏳ Starting...")

    def _on_quit(self, _widget):
        Gtk.main_quit()

    def run(self):
        Gtk.main()


def check_icons():
    """Verify icons exist; fall back to theme icons if not."""
    global ICON_ON, ICON_OFF
    if not os.path.exists(os.path.join(ICON_DIR, "camera-on.svg")):
        ICON_ON  = "camera-web"
        ICON_OFF = "camera-disabled"


def acquire_lock() -> bool:
    """Return False if another instance is already running."""
    import fcntl
    try:
        fd = open(LOCK_FILE, "w")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.write(str(os.getpid()))
        fd.flush()
        # Keep fd open — lock is held as long as the process lives
        acquire_lock._fd = fd  # type: ignore[attr-defined]
        return True
    except OSError:
        return False


def main():
    if not acquire_lock():
        print("privacy-tray is already running, exiting")
        sys.exit(0)
    check_icons()
    tray = PrivacyTray()
    tray.run()


if __name__ == "__main__":
    main()
