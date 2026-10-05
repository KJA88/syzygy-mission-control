"""Polar capture hold, Wi-Fi recovery, and Start/Stop. No radio and no strap."""
import json
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile

import ui.server as server
from control.owner import SkillOwner
from control.polar_capture import (
    HOLD_REASON,
    MORNING_MAX_SECONDS,
    RESTORE_REASON,
    WORKOUT_SAFETY_SECONDS,
    PolarCapture,
    collector_command,
    parse_collector_output,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = 1800000000
SAMPLE = (
    '{"errors": 0, "found": true, "hr_bpm": 77.35, "hrv_rmssd_ms": 17.01, '
    '"measurements_appended": 68, "reconnects": 0, "rr_count": 66, '
    '"rr_rejected": 0, "seconds": 62.26}\n'
)


class FakePort:
    def __init__(self):
        self.calls = []
        self.release = None
        self.started = None

    def available(self):
        return True

    def execute(self, skill, params):
        self.calls.append(skill)
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            self.release.wait(2)
        return {"ok": True, "reason": "PATTERN_FINISHED"}

    def stop(self):
        return {"ok": False, "stopped": True}

    def engineering(self, packet):
        self.calls.append("engineering")
        return {"ok": True}


class FakeRadio:
    def __init__(self, path=None):
        self.path = path
        self.enabled = True
        self.connection = "roarm-ap"
        self.address = "192.168.4.2/24"
        self.events = []
        self.fail_restore = False
        self.phase_at_disable = None

    def snapshot(self):
        if not self.enabled:
            return {"radio": "disabled", "connection": "", "address": ""}
        return {
            "radio": "enabled",
            "connection": self.connection,
            "address": self.address,
        }

    def disable(self):
        if self.path is not None:
            self.phase_at_disable = json.loads(self.path.read_text(encoding="utf-8"))["phase"]
        self.events.append("disable")
        self.enabled = False

    def restore(self, connection):
        self.events.append(("restore", connection))
        if self.fail_restore:
            return
        self.enabled = True
        self.connection = connection
        self.address = "192.168.4.2/24"


class Script:
    def __init__(self, code=0, output=SAMPLE, hang=False):
        self.code = code
        self._output = output
        self.hang = hang
        self.started = threading.Event()
        self.killed = threading.Event()
        self.seconds = None
        self.mode = None
        self.terminated = False
        self.pid = 4321
        self.calls = 0
        self.wait_timeout = None

    def __call__(self, mode, seconds):
        self.mode = mode
        self.seconds = seconds
        self.calls += 1
        self.started.set()
        return self

    def wait(self, timeout):
        self.wait_timeout = timeout
        if self.hang:
            if not self.killed.wait(timeout):
                return None
            return 0 if self.terminated else -9
        return self.code

    def kill(self):
        self.killed.set()

    def terminate(self):
        self.terminated = True
        self.killed.set()

    def output(self):
        return self._output


def make_owner(directory, port=None):
    return SkillOwner(
        Path(directory) / "operational-state.json",
        port or FakePort(),
        lambda: {"reachable": True, "fresh": True},
        lambda: NOW,
    )


def make_capture(directory, owner, radio=None, runner=None, **kwargs):
    path = Path(directory) / "polar-capture.json"
    radio = radio or FakeRadio(path)
    radio.path = path
    runner = runner or Script()
    capture = PolarCapture(
        owner,
        path,
        ethernet=kwargs.pop("ethernet", lambda: True),
        radio=radio,
        runner=runner,
        overhead_s=kwargs.pop("overhead_s", 0.05),
        **kwargs,
    )
    return capture, radio, runner


class PolarCaptureTests(unittest.TestCase):
    def test_collector_commands_keep_morning_hrv_and_workout_apart(self):
        morning = collector_command(ROOT, "morning_hrv", 60)
        workout = collector_command(ROOT, "workout", WORKOUT_SAFETY_SECONDS)
        self.assertEqual(morning[2], "resting")
        self.assertEqual(morning[3], "60")
        self.assertEqual(workout[2], "workout")
        self.assertEqual(workout[3], "10800")
        self.assertEqual(WORKOUT_SAFETY_SECONDS, 3 * 60 * 60)
        self.assertEqual(MORNING_MAX_SECONDS, 900)
        self.assertNotEqual(morning[2], workout[2])
        parsed = parse_collector_output("noise\n" + SAMPLE)
        self.assertEqual(parsed["rr_count"], 66)
        self.assertNotIn("token", parsed)

    def test_second_start_and_skills_are_rejected_while_held(self):
        with tempfile.TemporaryDirectory() as directory:
            port = FakePort()
            owner = make_owner(directory, port)
            script = Script(hang=True)
            capture, radio, _runner = make_capture(directory, owner, runner=script)
            started = capture.start(operator="kja", seconds=30)
            self.assertTrue(script.started.wait(1))
            self.assertTrue(started["accepted"])
            self.assertEqual(capture.start(operator="kja", seconds=30)["reason"], "CAPTURE_ACTIVE")
            skill = owner.request("return_home", authority="manual", operator="manual")
            raw = owner.engineering({"T": 105}, authority="manual", operator="manual")
            self.assertEqual(skill["reason"], HOLD_REASON)
            self.assertEqual(raw["reason"], HOLD_REASON)
            self.assertEqual(radio.phase_at_disable, "disabling")
            self.assertEqual(radio.events[0], "disable")
            stopped = capture.stop(operator="kja")
            self.assertEqual(stopped["phase"], "idle")
            self.assertIsNone(owner.hold())
            self.assertTrue(radio.enabled)
            self.assertIn(("restore", "roarm-ap"), radio.events)

    def test_normal_completion_restores_wifi_and_releases_the_arm(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = make_owner(directory)
            capture, radio, runner = make_capture(directory, owner)
            self.assertTrue(capture.start(operator="kja", seconds=60)["accepted"])
            status = capture.wait_until(("idle", "failed_restore"), timeout=2)
            self.assertEqual(status["phase"], "idle")
            self.assertTrue(status["arm_available"])
            self.assertIsNone(owner.hold())
            self.assertEqual(runner.seconds, 60)
            self.assertIn("66", status["message"])
            self.assertIn("17.01", status["message"])
            self.assertNotIn("not established", status["message"])
            self.assertTrue(radio.enabled)
            self.assertEqual(radio.connection, "roarm-ap")
            again = capture.recover()
            self.assertEqual(again["phase"], "idle")
            self.assertEqual(radio.events.count("disable"), 1)
            self.assertEqual(radio.events.count(("restore", "roarm-ap")), 1)

    def test_completion_without_rr_does_not_claim_hrv(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = make_owner(directory)
            runner = Script(output='{"hr_bpm": 70, "rr_count": 0, "seconds": 60, "hrv_rmssd_ms": null}\n')
            capture, _radio, _runner = make_capture(directory, owner, runner=runner)
            capture.start(operator="kja", seconds=60)
            status = capture.wait_until(("idle",), timeout=2)
            self.assertIn("HRV was not established", status["message"])
            self.assertIn("no RR samples", status["message"])

    def test_cancellation_kills_the_collector_and_restores(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = make_owner(directory)
            script = Script(hang=True)
            capture, radio, _runner = make_capture(directory, owner, runner=script)
            capture.start(operator="kja", seconds=60)
            self.assertTrue(script.started.wait(1))
            status = capture.stop(operator="kja")
            self.assertTrue(script.killed.is_set())
            self.assertEqual(status["phase"], "idle")
            self.assertIn("Morning HRV stopped", status["message"])
            self.assertFalse(script.terminated)
            self.assertIsNone(owner.hold())
            self.assertTrue(radio.enabled)

    def test_timeout_kills_a_hung_collector_and_restores(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = make_owner(directory)
            script = Script(hang=True)
            capture, radio, _runner = make_capture(directory, owner, runner=script, overhead_s=0.05)
            capture.start(operator="kja", seconds=1)
            status = capture.wait_until(("idle", "failed_restore"), timeout=3)
            self.assertTrue(script.killed.is_set())
            self.assertEqual(status["phase"], "idle")
            self.assertIn("timed out", status["message"])
            self.assertTrue(radio.enabled)
            self.assertIsNone(owner.hold())

    def test_capture_failure_restores_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = make_owner(directory)
            runner = Script(code=3, output='{"errors": 1, "rr_count": 0}\n')
            capture, radio, _runner = make_capture(directory, owner, runner=runner)
            capture.start(operator="kja", seconds=60)
            status = capture.wait_until(("idle",), timeout=2)
            self.assertIn("Capture failed", status["message"])
            self.assertTrue(radio.enabled)
            self.assertIsNone(owner.hold())
            self.assertTrue(owner.request("return_home", authority="manual", operator="manual")["accepted"])

    def test_restart_recovers_wifi_before_releasing_the_arm(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = make_owner(directory)
            path = Path(directory) / "polar-capture.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "phase": "capturing",
                "connection": "roarm-ap",
                "address": "192.168.4.2/24",
                "pid": 4242,
            }), encoding="utf-8")
            radio = FakeRadio(path)
            radio.enabled = False
            killed = []
            capture, _radio, _runner = make_capture(
                directory,
                owner,
                radio=radio,
                killer=lambda pid: killed.append(pid),
            )
            status = capture.recover()
            self.assertEqual(killed, [4242])
            self.assertEqual(status["phase"], "idle")
            self.assertTrue(radio.enabled)
            self.assertEqual(radio.connection, "roarm-ap")
            self.assertIsNone(owner.hold())
            self.assertIn("recovery restored", status["message"])

    def test_failed_restore_keeps_the_arm_until_a_later_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = make_owner(directory)
            radio = FakeRadio()
            radio.fail_restore = True
            capture, _radio, _runner = make_capture(directory, owner, radio=radio)
            capture.start(operator="kja", seconds=60)
            status = capture.wait_until(("failed_restore",), timeout=2)
            self.assertEqual(status["phase"], "failed_restore")
            self.assertFalse(status["arm_available"])
            self.assertIn("Arm unavailable", status["message"])
            self.assertIn("roarm-ap", status["message"])
            self.assertIn("192.168.4.2", status["message"])
            self.assertEqual(owner.hold()["reason"], RESTORE_REASON)
            blocked = owner.request("return_home", authority="manual", operator="manual")
            self.assertEqual(blocked["reason"], RESTORE_REASON)
            self.assertEqual(capture.start(operator="kja")["reason"], RESTORE_REASON)
            retry = capture.stop(operator="kja")
            self.assertEqual(retry["phase"], "failed_restore")
            radio.fail_restore = False
            restored = capture.stop(operator="kja")
            self.assertEqual(restored["phase"], "idle")
            self.assertIsNone(owner.hold())
            self.assertTrue(radio.enabled)

    def test_ethernet_or_busy_arm_does_not_disable_wifi(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = make_owner(directory)
            capture, radio, _runner = make_capture(directory, owner, ethernet=lambda: False)
            rejected = capture.start(operator="kja", seconds=60)
            self.assertEqual(rejected["reason"], "ETHERNET_UNAVAILABLE")
            self.assertEqual(radio.events, [])
            self.assertIsNone(owner.hold())
            radio.connection = "other"
            capture.ethernet = lambda: True
            rejected = capture.start(operator="kja", seconds=60)
            self.assertEqual(rejected["reason"], "WIFI_NOT_READY")
            self.assertEqual(radio.events, [])

        with tempfile.TemporaryDirectory() as directory:
            port = FakePort()
            port.release = threading.Event()
            port.started = threading.Event()
            owner = make_owner(directory, port)
            capture, radio, _runner = make_capture(directory, owner)
            worker = threading.Thread(
                target=lambda: owner.request("return_home", authority="manual", operator="manual")
            )
            worker.start()
            self.assertTrue(port.started.wait(1))
            rejected = capture.start(operator="kja", seconds=60)
            self.assertEqual(rejected["reason"], "ARM_ACTIVE")
            self.assertEqual(radio.events, [])
            port.release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())

    def test_missing_wifi_privilege_does_not_hold_the_arm(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = make_owner(directory)
            radio = FakeRadio()
            radio.available = lambda: False
            capture, _radio, _runner = make_capture(directory, owner, radio=radio)
            rejected = capture.start(operator="kja", seconds=60)
            self.assertEqual(rejected["reason"], "WIFI_PRIVILEGE_UNAVAILABLE")
            self.assertEqual(radio.events, [])
            self.assertIsNone(owner.hold())

    def test_perception_and_blank_operator_cannot_start(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = make_owner(directory)
            capture, radio, _runner = make_capture(directory, owner)
            self.assertEqual(capture.start(operator="perception")["reason"], "TRIGGER_NOT_ENABLED")
            self.assertEqual(capture.start(operator="  ")["reason"], "OPERATOR_REQUIRED")
            self.assertEqual(capture.start(operator="kja", seconds=0)["reason"], "MALFORMED_PARAMETERS")
            self.assertEqual(capture.start(operator="kja", seconds=901)["reason"], "MALFORMED_PARAMETERS")
            self.assertEqual(radio.events, [])

    def test_simultaneous_starts_disable_wifi_once(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = make_owner(directory)
            script = Script(hang=True)
            capture, radio, _runner = make_capture(directory, owner, runner=script)
            barrier = threading.Barrier(2)
            results = []

            def go():
                barrier.wait()
                results.append(capture.start(operator="kja", seconds=30))

            threads = [threading.Thread(target=go), threading.Thread(target=go)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
            accepted = [item for item in results if item["accepted"]]
            self.assertEqual(len(accepted), 1)
            self.assertEqual(radio.events.count("disable"), 1)
            capture.stop(operator="kja")
            self.assertIsNone(owner.hold())

    def test_capture_and_skill_cannot_both_succeed(self):
        with tempfile.TemporaryDirectory() as directory:
            port = FakePort()
            owner = make_owner(directory, port)
            capture, radio, _runner = make_capture(directory, owner)
            for _index in range(20):
                port.release = threading.Event()
                port.started = threading.Event()
                script = Script(hang=True)
                capture.runner = script
                barrier = threading.Barrier(2)
                results = {}

                def do_skill():
                    barrier.wait()
                    results["skill"] = owner.request(
                        "return_home", authority="manual", operator="manual"
                    )

                def do_capture():
                    barrier.wait()
                    results["capture"] = capture.start(operator="kja", seconds=30)

                threads = [threading.Thread(target=do_skill), threading.Thread(target=do_capture)]
                for thread in threads:
                    thread.start()
                deadline = threading.Event()
                for _spin in range(200):
                    if "skill" in results and "capture" in results:
                        break
                    if port.started.is_set() and "capture" in results:
                        break
                    deadline.wait(0.01)
                port.release.set()
                if results.get("capture", {}).get("accepted"):
                    capture.stop(operator="kja")
                for thread in threads:
                    thread.join(2)
                self.assertIn("skill", results)
                self.assertIn("capture", results)
                self.assertFalse(results["skill"]["accepted"] and results["capture"]["accepted"])
                self.assertTrue(results["skill"]["accepted"] or results["capture"]["accepted"])
                self.assertIsNone(owner.hold())
            self.assertTrue(radio.enabled)

    def test_workout_duration_end_finalizes_like_stop(self):
        output = (
            '{"mode": "workout", "hr_bpm": 140, "seconds": 900, '
            '"training_appended": 1, "measurements_appended": 0, "rr_count": 0}\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            owner = make_owner(directory)
            script = Script(code=0, output=output)
            capture, radio, _runner = make_capture(directory, owner, runner=script)
            self.assertTrue(capture.start(operator="kja", mode="workout")["accepted"])
            finished = capture.wait_until(("idle", "failed_restore"), timeout=2)
            self.assertEqual(finished["phase"], "idle")
            self.assertFalse(script.terminated)
            self.assertEqual(finished["last"]["training_appended"], 1)
            self.assertEqual(finished["last"]["measurements_appended"], 0)
            self.assertIn("Saved to Training as POLAR_H10", finished["message"])
            self.assertIsNone(owner.hold())
            self.assertTrue(radio.enabled)
            duration_message = finished["message"]

        with tempfile.TemporaryDirectory() as directory:
            owner = make_owner(directory)
            script = Script(hang=True, output=output)
            capture, radio, _runner = make_capture(directory, owner, runner=script, overhead_s=0.05)
            self.assertTrue(capture.start(operator="kja", mode="workout", seconds=1)["accepted"])
            self.assertTrue(script.started.wait(1))
            timed_out = capture.wait_until(("idle", "failed_restore"), timeout=3)
            self.assertTrue(script.terminated)
            self.assertEqual(timed_out["phase"], "idle")
            self.assertEqual(timed_out["last"]["training_appended"], 1)
            self.assertEqual(timed_out["last"]["measurements_appended"], 0)
            self.assertEqual(timed_out["message"], duration_message)
            self.assertIn("Saved to Training as POLAR_H10", timed_out["message"])
            self.assertNotIn("timed out", timed_out["message"])
            self.assertIsNone(owner.hold())
            self.assertTrue(radio.enabled)

    def test_workout_stop_writes_training_and_leaves_morning_hrv_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = make_owner(directory)
            script = Script(
                hang=True,
                output='{"mode": "workout", "hr_bpm": 140, "seconds": 600, "training_appended": 1, "measurements_appended": 0, "rr_count": 0}\n',
            )
            capture, radio, _runner = make_capture(directory, owner, runner=script)
            started = capture.start(operator="kja", mode="workout")
            self.assertTrue(started["accepted"])
            self.assertTrue(script.started.wait(1))
            self.assertEqual(script.mode, "workout")
            self.assertEqual(script.seconds, WORKOUT_SAFETY_SECONDS)
            self.assertGreater(script.seconds, 15 * 60)
            self.assertEqual(capture.status()["mode"], "workout")
            blocked = capture.start(operator="kja", mode="morning_hrv", seconds=60)
            self.assertEqual(blocked["reason"], "CAPTURE_ACTIVE")
            status = capture.stop(operator="kja")
            self.assertTrue(script.terminated)
            self.assertFalse(script.killed.is_set() and not script.terminated)
            self.assertEqual(status["phase"], "idle")
            self.assertIn("Saved to Training as POLAR_H10", status["message"])
            self.assertNotIn("Morning HRV", status["message"])
            self.assertIsNone(capture.status()["mode"])
            self.assertTrue(radio.enabled)
            self.assertIsNone(owner.hold())

    def test_duplicate_workout_start_is_rejected_without_a_second_collector(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = make_owner(directory)
            script = Script(hang=True)
            capture, _radio, _runner = make_capture(directory, owner, runner=script)
            first = capture.start(operator="kja", mode="workout")
            self.assertTrue(first["accepted"])
            self.assertTrue(script.started.wait(1))
            second = capture.start(operator="other", mode="workout")
            morning = capture.start(operator="kja", mode="morning_hrv", seconds=60)
            self.assertEqual(second["reason"], "CAPTURE_ACTIVE")
            self.assertEqual(second["mode"], "workout")
            self.assertFalse(second["accepted"])
            self.assertEqual(morning["reason"], "CAPTURE_ACTIVE")
            self.assertEqual(script.calls, 1)
            self.assertEqual(script.seconds, WORKOUT_SAFETY_SECONDS)
            capture.stop(operator="kja")

    def test_operator_stop_after_more_than_fifteen_minutes_finalizes(self):
        output = (
            '{"mode": "workout", "hr_bpm": 127.22, "seconds": 1200, '
            '"training_appended": 1, "measurements_appended": 0, "rr_count": 0}\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            owner = make_owner(directory)
            script = Script(hang=True, output=output)
            capture, radio, _runner = make_capture(directory, owner, runner=script)
            self.assertTrue(capture.start(operator="kja", mode="workout")["accepted"])
            self.assertTrue(script.started.wait(1))
            self.assertEqual(script.seconds, WORKOUT_SAFETY_SECONDS)
            for _ in range(50):
                if script.wait_timeout is not None:
                    break
                time.sleep(0.01)
            self.assertGreater(script.wait_timeout, 15 * 60)
            self.assertEqual(capture.status()["phase"], "capturing")
            self.assertFalse(script.terminated)
            status = capture.stop(operator="kja")
            self.assertTrue(script.terminated)
            finished = capture.status()
            self.assertEqual(status["phase"], "idle")
            self.assertEqual(finished["last"]["training_appended"], 1)
            self.assertEqual(finished["last"]["measurements_appended"], 0)
            self.assertIn("Saved to Training as POLAR_H10", finished["message"])
            self.assertIsNone(owner.hold())
            self.assertTrue(radio.enabled)

    def test_three_hour_safety_timeout_finalizes_like_stop(self):
        output = (
            '{"mode": "workout", "hr_bpm": 140, "seconds": 10800, '
            '"training_appended": 1, "measurements_appended": 0, "rr_count": 0}\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            owner = make_owner(directory)
            script = Script(code=0, output=output)
            capture, _radio, _runner = make_capture(directory, owner, runner=script)
            self.assertTrue(capture.start(operator="kja", mode="workout")["accepted"])
            finished = capture.wait_until(("idle",), timeout=2)
            self.assertEqual(script.seconds, WORKOUT_SAFETY_SECONDS)
            self.assertIn("Saved to Training as POLAR_H10", finished["message"])
            natural = finished["message"]
        with tempfile.TemporaryDirectory() as directory:
            owner = make_owner(directory)
            script = Script(hang=True, output=output)
            capture, _radio, _runner = make_capture(directory, owner, runner=script, overhead_s=0.05)
            self.assertTrue(capture.start(operator="kja", mode="workout")["accepted"])
            self.assertTrue(script.started.wait(1))
            for _ in range(50):
                if script.wait_timeout is not None:
                    break
                time.sleep(0.01)
            self.assertEqual(script.wait_timeout, WORKOUT_SAFETY_SECONDS + 0.05)
            capture.stop(operator="kja")
        with tempfile.TemporaryDirectory() as directory:
            owner = make_owner(directory)
            script = Script(hang=True, output=output)
            capture, radio, _runner = make_capture(directory, owner, runner=script, overhead_s=0.05)
            self.assertTrue(capture.start(operator="kja", mode="workout", seconds=1)["accepted"])
            self.assertTrue(script.started.wait(1))
            timed_out = capture.wait_until(("idle", "failed_restore"), timeout=3)
            self.assertTrue(script.terminated)
            self.assertEqual(timed_out["message"], natural)
            self.assertEqual(timed_out["last"]["training_appended"], 1)
            self.assertEqual(timed_out["last"]["measurements_appended"], 0)
            self.assertNotIn("timed out", timed_out["message"])
            self.assertIsNone(owner.hold())
            self.assertTrue(radio.enabled)

    def test_morning_hrv_stays_a_short_timed_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = make_owner(directory)
            script = Script(hang=True)
            capture, _radio, _runner = make_capture(directory, owner, runner=script)
            rejected = capture.start(operator="kja", mode="morning_hrv", seconds=WORKOUT_SAFETY_SECONDS)
            self.assertEqual(rejected["reason"], "MALFORMED_PARAMETERS")
            self.assertEqual(script.calls, 0)
            started = capture.start(operator="kja", mode="morning_hrv")
            self.assertTrue(started["accepted"])
            self.assertTrue(script.started.wait(1))
            self.assertEqual(script.mode, "morning_hrv")
            self.assertEqual(script.seconds, 60)
            self.assertLessEqual(script.seconds, MORNING_MAX_SECONDS)
            capture.stop(operator="kja")

    def test_http_start_stop_and_rejected_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = make_owner(directory)
            script = Script(hang=True)
            capture, radio, _runner = make_capture(directory, owner, runner=script)
            handler = server.make_handler(
                Path(directory), ROOT / "ui", 120, 20, owner=owner, polar=capture,
            )
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            base = "http://127.0.0.1:%d" % httpd.server_port
            try:
                extra = _post(base + "/api/polar/capture/start", {"operator": "kja", "source": "perception"})
                self.assertEqual(extra["reason"], "MALFORMED_PARAMETERS")
                self.assertEqual(radio.events, [])
                missing = _post(base + "/api/polar/capture/start", {})
                self.assertEqual(missing["reason"], "OPERATOR_REQUIRED")
                started = _post(base + "/api/polar/capture/start", {"operator": "kja", "seconds": 60})
                self.assertTrue(started["accepted"])
                with urllib.request.urlopen(base + "/api/polar/capture", timeout=2) as response:
                    status = json.loads(response.read().decode("utf-8"))
                self.assertEqual(status["phase"], "capturing")
                self.assertFalse(status["arm_available"])
                self.assertTrue(str(status.get("started_at") or "").endswith("Z"))
                duplicate = _post(base + "/api/polar/capture/start", {"operator": "kja", "mode": "workout"})
                self.assertFalse(duplicate["accepted"])
                self.assertEqual(duplicate["reason"], "CAPTURE_ACTIVE")
                self.assertEqual(duplicate["mode"], "morning_hrv")
                self.assertEqual(script.calls, 1)
                stopped = _post(base + "/api/polar/capture/stop", {"operator": "kja"})
                self.assertEqual(stopped["phase"], "idle")
                page = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
                script_text = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
                self.assertIn('id="polar-morning-start"', page)
                self.assertIn(">Start Morning HRV<", page)
                self.assertIn('id="polar-morning-stop"', page)
                self.assertIn('id="polar-workout-start"', page)
                self.assertIn(">Start workout<", page)
                self.assertIn('id="polar-workout-stop"', page)
                self.assertIn(">Stop workout<", page)
                self.assertIn('id="polar-arm-status"', page)
                self.assertNotIn("polar-start\"", page)
                self.assertIn("mode: \"morning_hrv\"", script_text)
                self.assertIn("mode: \"workout\"", script_text)
                self.assertIn("/api/polar/capture/start", script_text)
                self.assertIn("/api/polar/capture/stop", script_text)
                self.assertIn("Workout recording...", script_text)
                self.assertIn("3-hour safety limit", page)
                self.assertIn("polarError", script_text)
                self.assertIn('id="polar-live"', page)
                rejected = _post(base + "/api/polar/capture/start", {"operator": "kja", "mode": "strap"})
                self.assertEqual(rejected["reason"], "MALFORMED_PARAMETERS")
            finally:
                capture.shutdown()
                httpd.shutdown()
                httpd.server_close()


def _post(url, payload):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
