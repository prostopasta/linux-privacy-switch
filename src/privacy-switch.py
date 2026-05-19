#!/usr/bin/env python3
"""
linux-privacy-switch — daemon for the hardware camera/microphone privacy switch.

Supported devices: see config/devices.conf
Run diagnostics: sudo python3 privacy-switch.py --detect
"""

import struct
import select
import os
import subprocess
import time
import logging
import sys
import pwd
import configparser
import argparse
import glob
import threading
import queue

DEFAULT_CONFIG_PATH = "/etc/linux-privacy-switch/devices.conf"
STATE_FILE          = "/var/lib/linux-privacy-switch/state"
HEARTBEAT_FILE      = "/var/lib/linux-privacy-switch/heartbeat"
PROBE_INTERVAL_S    = 60   # periodic V4L2 health check interval
HEARTBEAT_INTERVAL_S = 10  # heartbeat write interval

EVENT_FORMAT = "llHHI"
EVENT_SIZE   = struct.calcsize(EVENT_FORMAT)
EV_KEY       = 1

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [privacy-switch] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ── DMI / device ──────────────────────────────────────────────────────────────

def dmi_read(field: str) -> str:
    try:
        with open(f"/sys/class/dmi/id/{field}") as f:
            return f.read().strip()
    except Exception:
        return ""


def find_input_event(device_name: str) -> str | None:
    for input_dir in sorted(glob.glob("/sys/class/input/input*/")):
        try:
            with open(os.path.join(input_dir, "name")) as f:
                if f.read().strip() == device_name:
                    for ev in glob.glob(os.path.join(input_dir, "event*/")):
                        return f"/dev/input/{os.path.basename(ev.rstrip('/'))}"
        except Exception:
            continue
    return None


def autodetect_device(config_path: str) -> dict | None:
    vendor  = dmi_read("sys_vendor")
    product = dmi_read("product_name")
    log.info("DMI: vendor=%r product=%r", vendor, product)

    cfg = configparser.ConfigParser()
    if not cfg.read(config_path):
        log.warning("Config file not found: %s", config_path)
        return None

    for section in cfg.sections():
        sv = cfg.get(section, "match_sys_vendor",   fallback="")
        pn = cfg.get(section, "match_product_name", fallback="")
        if sv and sv.lower() in vendor.lower() and pn.lower() in product.lower():
            profile = dict(cfg[section])
            profile["_section"] = section
            log.info("Profile: %s", section)
            return profile

    log.warning("Device not recognized. Run: sudo python3 privacy-switch.py --detect")
    return None


# ── Diagnostics mode ──────────────────────────────────────────────────────────

def detect_mode(config_path: str = DEFAULT_CONFIG_PATH):
    print("=" * 60)
    print("linux-privacy-switch — device profile detection")
    print("=" * 60)
    print(f"  sys_vendor:   {dmi_read('sys_vendor')}")
    print(f"  product_name: {dmi_read('product_name')}")
    print(f"  product_ver:  {dmi_read('product_version')}")
    print()
    print("Input devices:")
    for input_dir in sorted(glob.glob("/sys/class/input/input*/")):
        try:
            with open(os.path.join(input_dir, "name")) as f:
                name = f.read().strip()
        except Exception:
            continue
        for ev in glob.glob(os.path.join(input_dir, "event*/")):
            node = f"/dev/input/{os.path.basename(ev.rstrip('/'))}"
            print(f"  {node:25s}  {name}")
    print()
    profile = autodetect_device(config_path)
    if profile:
        print(f"Profile: {profile['_section']}")
    else:
        print("Next step: sudo python3 privacy-switch.py --monitor")
        print("  Move the slider — you will see device_name and key_code.")
        print()
        print("Template for config/devices.conf:\n")
        print(f"[my-laptop-model]")
        print(f"match_sys_vendor       = {dmi_read('sys_vendor')}")
        print(f"match_product_name     = {dmi_read('product_name')}")
        print(f"input_device_name      = <device name from the table above>")
        print(f"key_code               = <key code from --monitor>")
        print(f"alsa_card              = 0")
        print(f"alsa_control           = Capture")


def monitor_mode():
    print("Monitoring events. Move the slider (Ctrl+C to exit)...\n")
    stop = threading.Event()

    def watch(path, name):
        try:
            fd = open(path, "rb")
        except Exception:
            return
        while not stop.is_set():
            r, _, _ = select.select([fd], [], [], 0.5)
            if r:
                data = fd.read(EVENT_SIZE)
                if len(data) < EVENT_SIZE:
                    break
                _, _, evtype, code, value = struct.unpack(EVENT_FORMAT, data)
                if evtype in (EV_KEY, 5) and value == 1:
                    print(f"  EVENT  device={name!r:45s}  type={evtype}  code={code} (0x{code:x})")
        fd.close()

    for input_dir in sorted(glob.glob("/sys/class/input/input*/")):
        try:
            with open(os.path.join(input_dir, "name")) as f:
                name = f.read().strip()
        except Exception:
            continue
        for ev in glob.glob(os.path.join(input_dir, "event*/")):
            node = f"/dev/input/{os.path.basename(ev.rstrip('/'))}"
            threading.Thread(target=watch, args=(node, name), daemon=True).start()

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        stop.set()


# ── State ─────────────────────────────────────────────────────────────────────

def state_load() -> bool:
    """
    Load last saved state.
    Defaults to OFF on first run (no state file).
    Supports both formats: "boot_id:val" (legacy) and "val" (current).
    """
    try:
        with open(STATE_FILE) as f:
            content = f.read().strip()
        val = content.split(":", 1)[1] if ":" in content else content
        enabled = (val == "1")
        log.info("State from file: %s", "on" if enabled else "off")
        return enabled
    except FileNotFoundError:
        log.info("No state file — defaulting to off")
    except Exception as e:
        log.warning("Reading state: %s", e)
    return False


def state_save(enabled: bool):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            f.write("1\n" if enabled else "0\n")
    except Exception as e:
        log.warning("Writing state: %s", e)


def heartbeat_write():
    try:
        os.makedirs(os.path.dirname(HEARTBEAT_FILE), exist_ok=True)
        with open(HEARTBEAT_FILE, "w") as f:
            f.write(str(int(time.time())))
    except Exception:
        pass


# ── Camera ────────────────────────────────────────────────────────────────────

def _camera_measure_brightness() -> float | None:
    """
    Capture one V4L2 MJPEG frame; return % nonzero bytes, or None on error/busy.
    Timeout (no frame in 3 s) returns 0.0 — sensor not outputting = slider OFF.
    """
    import fcntl as _fcntl
    import mmap as _mmap

    VIDIOC_QUERYCAP  = 0x80685600
    VIDIOC_S_FMT     = 0xC0D05605
    VIDIOC_REQBUFS   = 0xC0145608
    VIDIOC_QUERYBUF  = 0xC0585609
    VIDIOC_QBUF      = 0xC058560F
    VIDIOC_DQBUF     = 0xC0585611
    VIDIOC_STREAMON  = 0x40045612
    VIDIOC_STREAMOFF = 0x40045613

    V4L2_CAP_VIDEO_CAPTURE = 0x00000001
    V4L2_BUF_TYPE      = 1
    V4L2_MEMORY_MMAP   = 1
    V4L2_PIX_FMT_MJPEG = 0x47504A4D

    for dev in sorted(glob.glob("/dev/video*")):
        fd = -1
        mm = None
        streamon_ok = False
        try:
            fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)

            cap_buf = bytearray(104)
            _fcntl.ioctl(fd, VIDIOC_QUERYCAP, cap_buf)
            if not (struct.unpack_from('<I', cap_buf, 64)[0] & V4L2_CAP_VIDEO_CAPTURE):
                continue

            fmt = bytearray(208)
            struct.pack_into('<I', fmt, 0,  V4L2_BUF_TYPE)
            struct.pack_into('<I', fmt, 8,  320)
            struct.pack_into('<I', fmt, 12, 240)
            struct.pack_into('<I', fmt, 16, V4L2_PIX_FMT_MJPEG)
            _fcntl.ioctl(fd, VIDIOC_S_FMT, fmt)

            rb = bytearray(16)
            struct.pack_into('<III', rb, 0, 1, V4L2_BUF_TYPE, V4L2_MEMORY_MMAP)
            _fcntl.ioctl(fd, VIDIOC_REQBUFS, rb)

            qb = bytearray(88)
            struct.pack_into('<I', qb, 0,  0)
            struct.pack_into('<I', qb, 4,  V4L2_BUF_TYPE)
            struct.pack_into('<I', qb, 56, V4L2_MEMORY_MMAP)
            _fcntl.ioctl(fd, VIDIOC_QUERYBUF, qb)
            buf_len    = struct.unpack_from('<I', qb, 72)[0]
            buf_offset = struct.unpack_from('<I', qb, 64)[0]

            mm = _mmap.mmap(fd, buf_len, _mmap.MAP_SHARED,
                            _mmap.PROT_READ | _mmap.PROT_WRITE, offset=buf_offset)
            _fcntl.ioctl(fd, VIDIOC_QBUF, qb)
            _fcntl.ioctl(fd, VIDIOC_STREAMON, struct.pack('<I', V4L2_BUF_TYPE))
            streamon_ok = True

            r, _, _ = select.select([fd], [], [], 3.0)
            if not r:
                log.info("camera_probe: %s timeout — no frame", dev)
                return 0.0

            _fcntl.ioctl(fd, VIDIOC_DQBUF, qb)
            mm.seek(0)
            data = mm.read(buf_len)
            zeros = data.count(b'\x00')
            return (len(data) - zeros) / len(data) * 100

        except OSError as e:
            if e.errno == 16:  # EBUSY
                log.info("camera_probe: %s busy (another app is using it)", dev)
                return None
            log.debug("camera_probe: %s: %s", dev, e)
            continue
        finally:
            try:
                if streamon_ok and fd >= 0:
                    import fcntl as _f
                    _f.ioctl(fd, VIDIOC_STREAMOFF, struct.pack('<I', V4L2_BUF_TYPE))
            except Exception:
                pass
            try:
                if mm:
                    mm.close()
            except Exception:
                pass
            if fd >= 0:
                try:
                    os.close(fd)
                except Exception:
                    pass

    return None  # no V4L2 devices found


def camera_probe_state(threshold: float = 3.0) -> bool | None:
    pct = _camera_measure_brightness()
    if pct is None:
        return None
    result = pct >= threshold
    log.info("camera_probe: %.1f%% nonzero → slider %s", pct, "ON" if result else "OFF")
    return result


def camera_set(enabled: bool, camera_power_path: str):
    if not camera_power_path:
        return
    try:
        with open(camera_power_path, "w") as f:
            f.write("1\n" if enabled else "0\n")
        log.info("camera_power → %d", 1 if enabled else 0)
    except Exception as e:
        log.warning("camera_set: %s", e)


# ── Microphone ────────────────────────────────────────────────────────────────

def mic_set(enabled: bool, alsa_card: str, alsa_control: str, user_uid: int | None, user_name: str):
    alsa_val = "cap" if enabled else "nocap"
    try:
        r = subprocess.run(
            ["amixer", "-c", alsa_card, "sset", alsa_control, alsa_val],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            log.warning("amixer sset %s %s: %s", alsa_control, alsa_val, r.stderr.strip())
        else:
            log.info("amixer sset %s → %s", alsa_control, alsa_val)
    except Exception as e:
        log.warning("mic_set amixer: %s", e)

    if not user_uid:
        return
    runtime_dir = f"/run/user/{user_uid}"
    wpctl_mute  = "0" if enabled else "1"
    try:
        r = subprocess.run(
            ["sudo", "-u", user_name,
             "env",
             f"DBUS_SESSION_BUS_ADDRESS=unix:path={runtime_dir}/bus",
             f"XDG_RUNTIME_DIR={runtime_dir}",
             "wpctl", "set-mute", "@DEFAULT_AUDIO_SOURCE@", wpctl_mute],
            capture_output=True, text=True, timeout=3,
        )
        if r.returncode != 0:
            log.warning("wpctl set-mute %s: %s", wpctl_mute, r.stderr.strip())
        else:
            log.info("wpctl → %s", "unmuted" if enabled else "muted")
    except Exception as e:
        log.warning("mic_set wpctl: %s", e)


# ── Notification ──────────────────────────────────────────────────────────────

def notify(enabled: bool, user_uid: int | None, user_name: str):
    if not user_uid:
        return
    icon    = "camera-web" if enabled else "camera-disabled"
    summary = "📷🎤 Camera and microphone ON" if enabled else "🚫🔇 Camera and microphone OFF"
    urgency = "normal" if enabled else "critical"
    runtime_dir = f"/run/user/{user_uid}"
    env = {
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime_dir}/bus",
        "XDG_RUNTIME_DIR":          runtime_dir,
        "DISPLAY":                  ":1",
        "XAUTHORITY":               f"{runtime_dir}/gdm/Xauthority",
        "HOME":                     f"/home/{user_name}",
        "PATH":                     "/usr/bin:/bin",
    }
    try:
        r = subprocess.run(
            ["sudo", "-u", user_name, "notify-send",
             "-i", icon, "-u", urgency, "-t", "4000", "Privacy", summary],
            env=env, timeout=5, capture_output=True, text=True,
        )
        if r.returncode != 0:
            log.warning("notify-send: %s", r.stderr.strip())
        else:
            log.info("notify: %s", summary)
    except Exception as e:
        log.warning("notify-send: %s", e)


# ── Config helpers ────────────────────────────────────────────────────────────

def _write_config_fields(config_path: str, section: str, fields: dict):
    """Update key=value pairs in an INI section in-place, preserving all comments."""
    try:
        with open(config_path) as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []

    in_section = False
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
                in_section = False
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

    with open(config_path, "w") as f:
        f.writelines(lines)


def calibrate_mode(config_path: str):
    """Interactively measure V4L2 brightness in OFF and ON states, suggest threshold."""
    profile = autodetect_device(config_path)
    if not profile:
        print("Device not recognized — cannot calibrate.")
        sys.exit(1)

    section = profile["_section"]
    has_hw_kill = profile.get("has_hw_camera_kill", "false").lower() == "true"

    print()
    print("=" * 60)
    print("Camera V4L2 threshold calibration")
    print("=" * 60)
    print(f"Profile: {section}")
    print()

    if not has_hw_kill:
        print("WARNING: This device has has_hw_camera_kill = false.")
        print("The camera sensor is always powered; brightness will not change")
        print("when the slider moves.  Continuing anyway (for testing).")
        print()

    print("Step 1 — OFF state")
    print("  Set the slider to OFF (camera disabled).")
    if not has_hw_kill:
        print("  Cover the camera lens with your finger during the measurement.")
    input("  Press Enter when ready...")
    pct_off = _camera_measure_brightness()
    if pct_off is None:
        print("ERROR: Could not read camera (busy or not found). Close other apps and retry.")
        sys.exit(1)
    print(f"  OFF brightness: {pct_off:.2f}% nonzero bytes")

    print()
    print("Step 2 — ON state")
    print("  Set the slider to ON (camera active, lens uncovered).")
    input("  Press Enter when ready...")
    pct_on = _camera_measure_brightness()
    if pct_on is None:
        print("ERROR: Could not read camera (busy or not found).")
        sys.exit(1)
    print(f"  ON  brightness: {pct_on:.2f}% nonzero bytes")

    gap = pct_on - pct_off
    threshold = (pct_off + pct_on) / 2.0

    print()
    print(f"  Gap:                 {gap:.2f}%")
    print(f"  Suggested threshold: {threshold:.1f}%")

    if gap < 1.0:
        print()
        print("WARNING: Gap < 1% — hardware kill may not actually cut sensor power.")
        print("  Try in a darker room, or verify the slider is fully in the OFF position.")
    elif gap < 2.0:
        print()
        print("WARNING: Gap 1–2% — marginal reading.")
        print("  Try a darker room or cover the lens more thoroughly for better accuracy.")

    print()
    ans = input(f"Save nonzero_threshold = {threshold:.1f} to config? [Y/n]: ").strip().lower()
    if ans in ("", "y", "yes"):
        _write_config_fields(config_path, section, {"nonzero_threshold": f"{threshold:.1f}"})
        print(f"Saved nonzero_threshold = {threshold:.1f} to {config_path}")
    else:
        print("Not saved.")


# ── Main loop ─────────────────────────────────────────────────────────────────

def run(profile: dict, user_name: str):
    input_dev          = find_input_event(profile["input_device_name"])
    key_code           = int(profile["key_code"])
    alsa_card          = profile.get("alsa_card", "0")
    alsa_control       = profile.get("alsa_control", "Capture")
    camera_power_path  = profile.get("camera_power_path", "")
    has_hw_camera_kill = profile.get("has_hw_camera_kill", "false").lower() == "true"
    sync_camera        = profile.get("sync_camera", "true").lower() == "true"
    sync_mic           = profile.get("sync_mic",    "true").lower() == "true"
    threshold          = float(profile.get("nonzero_threshold", "3.0"))

    if not input_dev:
        log.error("Input device not found: %r. Run --detect.", profile["input_device_name"])
        sys.exit(1)

    try:
        user_uid = pwd.getpwnam(user_name).pw_uid
    except Exception:
        user_uid = None
        log.warning("User %r not found — notifications disabled", user_name)

    last_toggle_time: float = 0.0
    DEBOUNCE_S = 0.3

    log.info("device=%s  key=%d  hw_kill=%s  sync_camera=%s  sync_mic=%s  threshold=%.1f",
             input_dev, key_code, has_hw_camera_kill, sync_camera, sync_mic, threshold)

    # At boot, wait until uptime >= 15s so the EC has time to apply the switch position to the sensor.
    # On service restart uptime is already large — skip the wait.
    try:
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
    except Exception:
        uptime = 999.0
    if uptime < 15:
        wait = 15.0 - uptime
        log.info("Boot: waiting %.1fs for EC to stabilize...", wait)
        time.sleep(wait)

    # Probe camera state via V4L2 (only on hardware-kill devices).
    # Software-only devices always have a powered sensor — probing is useless.
    if has_hw_camera_kill:
        log.info("V4L2 probe...")
        probed = camera_probe_state(threshold)
        if probed is not None:
            cam_enabled = probed
            state_save(cam_enabled)
            log.info("Initial state (V4L2): %s", "on" if cam_enabled else "off")
        else:
            cam_enabled = state_load()
            log.info("Initial state (file): %s", "on" if cam_enabled else "off")
    else:
        log.info("Software-only mode — using saved state (no V4L2 probe)")
        cam_enabled = state_load()

    if sync_camera:
        camera_set(cam_enabled, camera_power_path)
    if sync_mic:
        mic_set(cam_enabled, alsa_card, alsa_control, user_uid, user_name)
    notify(cam_enabled, user_uid, user_name)
    heartbeat_write()

    # Queue for results from the background V4L2 probe thread
    probe_q: queue.Queue = queue.Queue()
    probe_running = False
    last_probe_t     = time.monotonic()
    last_heartbeat_t = time.monotonic()
    # Require 2 consecutive disagreements before correcting state — prevents
    # a single borderline V4L2 measurement from flipping state incorrectly.
    probe_disagree_count = 0

    while True:
        try:
            fd = open(input_dev, "rb", buffering=0)
        except (PermissionError, FileNotFoundError) as e:
            log.error("%s: %s — retrying in 5s", input_dev, e)
            time.sleep(5)
            continue

        # Drain EC init-events that accumulated in the buffer during the wait.
        drained = 0
        while select.select([fd], [], [], 0)[0]:
            fd.read(EVENT_SIZE)
            drained += 1
        log.info("Listening on %s (drained: %d events)", input_dev, drained)

        try:
            while True:
                r, _, _ = select.select([fd], [], [], 1.0)
                now_t = time.monotonic()

                # Write heartbeat every HEARTBEAT_INTERVAL_S seconds
                if now_t - last_heartbeat_t >= HEARTBEAT_INTERVAL_S:
                    last_heartbeat_t = now_t
                    heartbeat_write()

                # Periodic V4L2 probe — only on hardware-kill devices
                if has_hw_camera_kill and not probe_running and now_t - last_probe_t >= PROBE_INTERVAL_S:
                    last_probe_t = now_t
                    probe_running = True
                    threading.Thread(
                        target=lambda q=probe_q: q.put(camera_probe_state(threshold)),
                        daemon=True,
                    ).start()

                # Collect result from background probe
                if probe_running:
                    try:
                        probed = probe_q.get_nowait()
                        probe_running = False
                        if probed is not None and probed != cam_enabled:
                            probe_disagree_count += 1
                            if probe_disagree_count >= 2:
                                log.warning(
                                    "State drift confirmed (%dx): probe=%s, state=%s — correcting",
                                    probe_disagree_count,
                                    "on" if probed else "off",
                                    "on" if cam_enabled else "off",
                                )
                                cam_enabled = probed
                                probe_disagree_count = 0
                                state_save(cam_enabled)
                                if sync_camera:
                                    camera_set(cam_enabled, camera_power_path)
                                if sync_mic:
                                    mic_set(cam_enabled, alsa_card, alsa_control, user_uid, user_name)
                                notify(cam_enabled, user_uid, user_name)
                            else:
                                log.info(
                                    "State drift suspected (probe=%s, state=%s) — waiting for confirmation",
                                    "on" if probed else "off",
                                    "on" if cam_enabled else "off",
                                )
                        elif probed is not None:
                            probe_disagree_count = 0
                            log.debug("Periodic probe: state is current (%s)",
                                      "on" if cam_enabled else "off")
                    except queue.Empty:
                        pass

                if not r:
                    continue

                data = fd.read(EVENT_SIZE)
                if len(data) < EVENT_SIZE:
                    log.warning("Short read — reopening device")
                    break
                _, _, evtype, code, value = struct.unpack(EVENT_FORMAT, data)
                if evtype == EV_KEY and code == key_code and value == 1:
                    elapsed = now_t - last_toggle_time
                    if elapsed < DEBOUNCE_S:
                        log.warning("Duplicate event (%.0fms) — skipping", elapsed * 1000)
                        continue
                    last_toggle_time = now_t
                    cam_enabled = not cam_enabled
                    probe_disagree_count = 0  # slider event is authoritative
                    log.info("Slider toggled → %s", "on" if cam_enabled else "off")
                    if sync_camera:
                        camera_set(cam_enabled, camera_power_path)
                    if sync_mic:
                        mic_set(cam_enabled, alsa_card, alsa_control, user_uid, user_name)
                    state_save(cam_enabled)
                    notify(cam_enabled, user_uid, user_name)
        except OSError as e:
            log.error("Error: %s — reopening in 2s", e)
            time.sleep(2)
        finally:
            fd.close()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Hardware privacy switch daemon")
    parser.add_argument("--detect",    action="store_true", help="Detect device profile")
    parser.add_argument("--monitor",   action="store_true", help="Monitor slider events")
    parser.add_argument("--calibrate", action="store_true",
                        help="Interactively calibrate V4L2 detection threshold (hardware kill devices only)")
    parser.add_argument("--config",    default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--user",      default=None, help="User for notifications (default: active session)")
    args = parser.parse_args()

    if args.detect:
        detect_mode(args.config)
        return
    if args.monitor:
        monitor_mode()
        return
    if args.calibrate:
        calibrate_mode(args.config)
        return

    profile = autodetect_device(args.config)
    if not profile:
        print("Device not found. Run: sudo python3 privacy-switch.py --detect")
        sys.exit(1)

    # Auto-detect user if not specified
    user_name = args.user
    if not user_name:
        import subprocess as sp
        r = sp.run(["loginctl", "list-sessions", "--no-legend"],
                   capture_output=True, text=True)
        for line in r.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3 and parts[3] != "root":
                user_name = parts[2]
                break
    if not user_name:
        user_name = os.environ.get("SUDO_USER", "")
    if not user_name:
        log.warning("Could not determine user — notifications disabled")

    run(profile, user_name)


if __name__ == "__main__":
    main()
