#!/usr/bin/env python3
"""
linux-privacy-switch — offline tests for commands run as the desktop user.

No session bus, no sudo, no desktop: these only check the shape of the command
line the daemon builds. The bug they guard against is passing the session bus
through subprocess.run(env=...) — sudo resets the environment, so the variables
never reach notify-send, which then tries to start its own bus via dbus-launch
and is denied by AppArmor.

Usage: python3 tests/test_user_commands.py
"""

import importlib.util
import pathlib
import unittest
from unittest import mock

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "privacy-switch.py"
_spec = importlib.util.spec_from_file_location("privacy_switch", SRC)
ps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ps)

UID  = 54435958
USER = "testuser"
BUS  = f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{UID}/bus"
RUNTIME = f"XDG_RUNTIME_DIR=/run/user/{UID}"


def _fake_run():
    """subprocess.run stub that records argv and keyword arguments."""
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return mock.Mock(returncode=0, stdout="", stderr="")

    return run, calls


class TestUserCommand(unittest.TestCase):
    def test_variables_are_arguments_of_env_not_the_sudo_environment(self):
        argv = ps.user_command(USER, UID, ["notify-send", "hello"])
        self.assertEqual(argv[:4], ["sudo", "-u", USER, "env"])
        self.assertIn(BUS, argv)
        self.assertIn(RUNTIME, argv)

    def test_assignments_come_before_the_program(self):
        argv = ps.user_command(USER, UID, ["notify-send", "-i", "camera-web", "hello"])
        program = argv.index("notify-send")
        for item in argv[4:program]:
            self.assertRegex(item, r"^[A-Z_][A-Z0-9_]*=")
        self.assertEqual(argv[program:], ["notify-send", "-i", "camera-web", "hello"])

    def test_extra_env_is_added(self):
        argv = ps.user_command(USER, UID, ["notify-send"], {"PATH": "/usr/bin:/bin"})
        self.assertIn("PATH=/usr/bin:/bin", argv)

    def test_home_comes_from_the_password_database(self):
        entry = mock.Mock(pw_dir="/home/domain/testuser")
        with mock.patch.object(ps.pwd, "getpwnam", return_value=entry):
            env = ps.session_env(UID, USER)
        self.assertEqual(env["HOME"], "/home/domain/testuser")

    def test_home_falls_back_when_the_user_is_unknown(self):
        with mock.patch.object(ps.pwd, "getpwnam", side_effect=KeyError):
            env = ps.session_env(UID, USER)
        self.assertEqual(env["HOME"], f"/home/{USER}")


class TestCallSites(unittest.TestCase):
    """Every command run through sudo must carry the session bus with it."""

    def _sudo_calls(self, calls):
        return [(argv, kwargs) for argv, kwargs in calls if argv[0] == "sudo"]

    def _assert_carries_the_bus(self, calls):
        sudo_calls = self._sudo_calls(calls)
        self.assertTrue(sudo_calls, "nothing was run as the desktop user")
        for argv, kwargs in sudo_calls:
            self.assertIn(BUS, argv)
            self.assertIn(RUNTIME, argv)
            # env= would be the sudo process's own environment, which sudo
            # resets — the regression this test exists for.
            self.assertIsNone(kwargs.get("env"))

    def test_notify(self):
        run, calls = _fake_run()
        with mock.patch.object(ps.subprocess, "run", run):
            ps.notify(False, UID, USER)
        self._assert_carries_the_bus(calls)

    def test_mic_set(self):
        run, calls = _fake_run()
        with mock.patch.object(ps.subprocess, "run", run):
            ps.mic_set(False, "1", "Capture", UID, USER)
        self._assert_carries_the_bus(calls)

    def test_nothing_runs_without_a_uid(self):
        run, calls = _fake_run()
        with mock.patch.object(ps.subprocess, "run", run):
            ps.notify(False, None, USER)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
