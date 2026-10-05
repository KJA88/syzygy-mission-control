"""Polar Wi-Fi helper. No radio, no strap, no sudo."""
import json
import tempfile
import unittest
from pathlib import Path

from control.owner import SkillOwner
from control.polar_capture import NmcliRadio, PolarCapture, RESTORE_REASON
from control.polar_wifi import (
    ACTIONS,
    ADDR,
    ALLOWED,
    CONNECTION_UP,
    DEVICE,
    RADIO,
    RADIO_OFF,
    RADIO_ON,
    PolarWifiActions,
    accept_line,
)

ROOT = Path(__file__).resolve().parents[1]
UID = 1001
SAMPLE = '{"hr_bpm": 77.35, "hrv_rmssd_ms": 17.01, "rr_count": 66, "seconds": 62.26}\n'


class World:
    def __init__(self):
        self.radio = "enabled"
        self.connection = "roarm-ap"
        self.address = "192.168.4.2/24"
        self.calls = []
        self.fail = set()

    def run(self, args, timeout=20):
        self.calls.append(args)
        if args in self.fail:
            return 1, "", "failed"
        if args == RADIO:
            return 0, self.radio + "\n", ""
        if args == DEVICE:
            if self.radio == "enabled" and self.connection:
                return 0, "wlan0:wifi:connected:%s\n" % self.connection, ""
            return 0, "wlan0:wifi:disconnected:\n", ""
        if args == ADDR:
            if self.radio == "enabled" and self.connection:
                return 0, self.address + "\n", ""
            return 0, "\n", ""
        if args == RADIO_OFF:
            self.radio = "disabled"
            self.connection = ""
            self.address = ""
            return 0, "", ""
        if args == RADIO_ON:
            self.radio = "enabled"
            return 0, "", ""
        if args == CONNECTION_UP:
            self.connection = "roarm-ap"
            self.address = "192.168.4.2/24"
            return 0, "", ""
        raise AssertionError(args)


def owner(directory):
    return SkillOwner(
        Path(directory) / "operational-state.json",
        _Port(),
        lambda: {"reachable": True, "fresh": True},
        lambda: 1800000000,
    )


class _Port:
    def available(self):
        return True

    def execute(self, skill, params):
        return {"ok": True}

    def stop(self):
        return {"ok": False, "stopped": True}

    def engineering(self, packet):
        return {"ok": True}


class HelperTests(unittest.TestCase):
    def test_helper_success_uses_only_fixed_commands(self):
        world = World()
        actions = PolarWifiActions(world.run)
        status = accept_line(b"status\n", UID, UID, actions)
        self.assertTrue(status["ok"])
        self.assertEqual(status["connection"], "roarm-ap")
        self.assertIn("192.168.4.2", status["address"])
        disabled = accept_line(b"disable\n", UID, UID, actions)
        self.assertTrue(disabled["ok"])
        down = actions.handle("status")
        self.assertEqual(down["radio"], "disabled")
        self.assertEqual(down["connection"], "")
        restored = accept_line(b"restore\n", UID, UID, actions)
        self.assertTrue(restored["ok"])
        verified = accept_line(b"verify\n", UID, UID, actions)
        self.assertTrue(verified["ok"])
        self.assertIn("192.168.4.2", verified["address"])
        self.assertTrue(set(world.calls).issubset(set(ALLOWED)))
        self.assertIn(RADIO_OFF, world.calls)
        self.assertIn(CONNECTION_UP, world.calls)
        self.assertNotIn("sudo", [part for args in world.calls for part in args])

    def test_helper_refuses_invalid_action_and_caller(self):
        world = World()
        actions = PolarWifiActions(world.run)
        for raw in (b"nmcli\n", b"disable extra\n", b"disable\nrestore\n", b"DISABLE\n", b"restore roarm-ap\n", b""):
            result = accept_line(raw, UID, UID, actions)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "rejected")
        stranger = accept_line(b"disable\n", 0, UID, actions)
        self.assertEqual(stranger["error"], "rejected")
        self.assertEqual(world.calls, [])
        self.assertEqual(actions.handle("wifi")["error"], "rejected")
        self.assertEqual(ACTIONS, ("status", "disable", "restore", "verify"))

    def test_disable_failure_leaves_the_recorded_state(self):
        world = World()
        world.fail.add(RADIO_OFF)
        result = PolarWifiActions(world.run).handle("disable")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "disable_failed")
        self.assertEqual(world.radio, "enabled")
        self.assertEqual(world.connection, "roarm-ap")

    def test_restore_failure(self):
        world = World()
        world.radio = "disabled"
        world.connection = ""
        world.address = ""
        world.fail.add(CONNECTION_UP)
        result = PolarWifiActions(world.run, restore_wait_s=0).handle("restore")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "restore_failed")
        self.assertIn(RADIO_ON, world.calls)
        self.assertIn(CONNECTION_UP, world.calls)
        self.assertNotEqual(world.connection, "roarm-ap")

    def test_restore_retries_until_wlan0_can_accept_roarm_ap(self):
        world = World()
        world.radio = "disabled"
        world.connection = ""
        world.address = ""
        world.up_fails_remaining = 1
        original = world.run

        def run(args, timeout=20):
            if args == CONNECTION_UP and world.up_fails_remaining:
                world.calls.append(args)
                world.up_fails_remaining -= 1
                return 1, "", "failed"
            return original(args, timeout)

        result = PolarWifiActions(run, restore_wait_s=5, sleep=lambda _seconds: None).handle("restore")
        self.assertTrue(result["ok"])
        self.assertEqual(world.calls.count(CONNECTION_UP), 2)
        self.assertEqual(world.connection, "roarm-ap")
        verified = PolarWifiActions(world.run).handle("verify")
        self.assertTrue(verified["ok"])

    def test_restore_does_not_succeed_before_the_address_is_assigned(self):
        world = World()
        world.radio = "disabled"
        world.connection = ""
        world.address = ""

        def run(args, timeout=20):
            if args == CONNECTION_UP:
                world.calls.append(args)
                world.radio = "enabled"
                world.connection = "roarm-ap"
                world.address = ""
                return 0, "", ""
            return world.run(args, timeout)

        result = PolarWifiActions(run, restore_wait_s=0, sleep=lambda _seconds: None).handle("restore")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "restore_failed")

    def test_verification_failure(self):
        world = World()
        world.address = "10.0.0.8/24"
        result = PolarWifiActions(world.run).handle("verify")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "verify_failed")
        self.assertNotIn(RADIO_OFF, world.calls)
        self.assertNotIn(CONNECTION_UP, world.calls)

    def test_radio_refuses_a_connection_other_than_roarm_ap(self):
        calls = []
        radio = NmcliRadio(client=lambda action: calls.append(action) or {"ok": True})
        radio.restore("other-network")
        self.assertEqual(calls, [])

    def test_restart_recovery_restores_through_the_helper_before_release(self):
        with tempfile.TemporaryDirectory() as directory:
            gate = owner(directory)
            state = {"radio": "disabled", "connection": "", "address": ""}
            calls = []

            def client(action):
                calls.append(action)
                if action == "restore":
                    state.update(radio="enabled", connection="roarm-ap", address="192.168.4.2/24")
                    return {"ok": True}
                if action == "status":
                    return {"ok": True, **state}
                return {"ok": False, "error": "rejected"}

            path = Path(directory) / "polar-capture.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "phase": "capturing",
                "connection": "roarm-ap",
                "address": "192.168.4.2/24",
                "pid": 4242,
            }), encoding="utf-8")
            capture = PolarCapture(
                gate,
                path,
                ethernet=lambda: True,
                radio=NmcliRadio(client=client),
                runner=lambda mode, seconds: None,
                killer=lambda pid: None,
            )
            status = capture.recover()
            self.assertIn("restore", calls)
            self.assertNotIn("disable", calls)
            self.assertEqual(status["phase"], "idle")
            self.assertIsNone(gate.hold())
            self.assertTrue(status["arm_available"])
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["phase"], "idle")

    def test_disable_and_restore_failures_follow_the_capture_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            gate = owner(directory)
            state = {"radio": "enabled", "connection": "roarm-ap", "address": "192.168.4.2/24"}
            calls = []

            def client(action):
                calls.append(action)
                if action == "disable":
                    return {"ok": False, "error": "disable_failed"}
                if action == "status":
                    return {"ok": True, **state}
                if action == "restore":
                    return {"ok": True}
                return {"ok": False, "error": "rejected"}

            capture = PolarCapture(
                gate,
                Path(directory) / "polar-capture.json",
                ethernet=lambda: True,
                radio=NmcliRadio(client=client),
                runner=lambda mode, seconds: (_ for _ in ()).throw(AssertionError("collector")),
            )
            failed = capture.start(operator="kja", seconds=60)
            self.assertEqual(failed["reason"], "WIFI_DISABLE_FAILED")
            self.assertIn("restore", calls)
            self.assertIsNone(gate.hold())

        with tempfile.TemporaryDirectory() as directory:
            gate = owner(directory)
            state = {"radio": "enabled", "connection": "roarm-ap", "address": "192.168.4.2/24"}

            class Done:
                pid = 9
                seconds = None

                def __call__(self, mode, seconds):
                    self.seconds = seconds
                    return self

                def wait(self, timeout):
                    state.update(radio="disabled", connection="", address="")
                    return 0

                def kill(self):
                    return None

                def output(self):
                    return SAMPLE

            def client(action):
                if action == "disable":
                    state.update(radio="disabled", connection="", address="")
                    return {"ok": True}
                if action == "restore":
                    return {"ok": False, "error": "restore_failed"}
                if action == "status":
                    return {"ok": True, **state}
                return {"ok": False, "error": "rejected"}

            capture = PolarCapture(
                gate,
                Path(directory) / "polar-capture.json",
                ethernet=lambda: True,
                radio=NmcliRadio(client=client),
                runner=Done(),
                overhead_s=0.05,
            )
            self.assertTrue(capture.start(operator="kja", seconds=60)["accepted"])
            status = capture.wait_until(("failed_restore",), timeout=2)
            self.assertEqual(status["phase"], "failed_restore")
            self.assertEqual(gate.hold()["reason"], RESTORE_REASON)
            self.assertFalse(status["arm_available"])
            self.assertIn("192.168.4.2", status["message"])


if __name__ == "__main__":
    unittest.main()
