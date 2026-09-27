"""Offline Phase 3 skill owner. No arm connection."""
import importlib.util
import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile

from control.owner import SkillOwner
from control.production import ProductionCommandPort, readiness_from_snapshot
from guardian.state_engine import load_published_operational

ROOT = Path(__file__).resolve().parents[1]
NOW = 1800000000


class FakePort:
    def __init__(self):
        self.calls = []
        self.outcome = {"ok": True, "reason": "PATTERN_FINISHED"}
        self.available_flag = True
        self.hook = None
        self.release = None
        self.started = None

    def available(self):
        return self.available_flag

    def execute(self, skill, params):
        self.calls.append(("execute", skill, dict(params)))
        if self.started is not None:
            self.started.set()
        if self.release is not None:
            self.release.wait(2)
        if self.hook is not None:
            self.hook(skill)
        return dict(self.outcome)

    def stop(self):
        self.calls.append(("stop",))
        return {"ok": False, "stopped": True, "reason": "PATTERN_STOP_REQUESTED"}

    def engineering(self, packet):
        self.calls.append(("engineering", dict(packet)))
        if self.hook is not None:
            self.hook("engineering")
        return dict(self.outcome)


def owner(directory, port=None, ready=None):
    path = Path(directory) / "operational-state.json"
    return SkillOwner(
        path,
        port or FakePort(),
        ready or (lambda: {"reachable": True, "fresh": True}),
        lambda: NOW,
    )


class SkillOwnerTests(unittest.TestCase):
    def test_valid_move_runs_and_returns_to_idle(self):
        port = FakePort()
        with tempfile.TemporaryDirectory() as directory:
            gate = owner(directory, port)
            result = gate.request(
                "move_to_pose",
                authority="manual",
                operator="manual",
                mission="mission-42",
                trace_id="trace-1",
                params={"x": 10, "y": -5, "z": 250},
            )
            self.assertEqual(result["result"], "completed")
            self.assertEqual(result["trace_id"], "trace-1")
            self.assertEqual(gate.states, ["IDLE", "PREPARING", "MOVING", "COMPLETE", "IDLE"])
            self.assertEqual(port.calls[0][0:2], ("execute", "move_to_pose"))
            view = gate.view()
            self.assertEqual(view["state"], "IDLE")
            self.assertIsNone(view["motion_authority"])
            self.assertFalse(view["motion_permitted"])
            self.assertEqual(view["last_completed_action"], "move_to_pose")
            self.assertIsNone(view["skill"])

    def test_unknown_malformed_and_workspace_do_not_acquire_authority(self):
        port = FakePort()
        with tempfile.TemporaryDirectory() as directory:
            gate = owner(directory, port)
            unknown = gate.request("gripper_open", authority="manual")
            malformed = gate.request("move_to_pose", authority="manual", params={"x": "no", "y": 0, "z": 0})
            outside = gate.request("move_to_pose", authority="manual", params={"x": 0, "y": 0, "z": 900})
            missing = gate.request("move_to_pose", authority="", params={"x": 0, "y": 0, "z": 100})
            self.assertEqual(unknown["reason"], "UNKNOWN_SKILL")
            self.assertEqual(malformed["reason"], "MALFORMED_PARAMETERS")
            self.assertEqual(outside["reason"], "OUTSIDE_WORKSPACE")
            self.assertEqual(missing["reason"], "AUTHORITY_REQUIRED")
            self.assertEqual(port.calls, [])
            self.assertEqual(gate.view()["state"], "IDLE")
            self.assertIsNone(gate.view()["motion_authority"])

    def test_pattern_home_and_event_request_use_the_same_api(self):
        port = FakePort()
        with tempfile.TemporaryDirectory() as directory:
            gate = owner(directory, port)
            home = gate.request("return_home", authority="home-assistant", operator="home-assistant")
            pattern = gate.request(
                "run_pattern",
                authority="camera-event-17",
                params={"pattern": "lissajous"},
            )
            bad = gate.request("run_pattern", authority="camera-event-17", params={"pattern": "scan_area"})
            self.assertEqual(home["result"], "completed")
            self.assertEqual(pattern["result"], "completed")
            self.assertEqual(bad["reason"], "MALFORMED_PARAMETERS")
            self.assertEqual([call[1] for call in port.calls], ["return_home", "run_pattern"])
            self.assertEqual(port.calls[1][2]["pattern"], "lissajous")

    def test_competing_authority_is_rejected_and_released_after_completion(self):
        port = FakePort()
        port.started = threading.Event()
        port.release = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            gate = owner(directory, port)
            box = {}

            def run():
                box["first"] = gate.request(
                    "move_to_pose",
                    authority="mission-42",
                    params={"x": 1, "y": 2, "z": 3},
                )

            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(port.started.wait(2))
            rival = gate.request(
                "return_home",
                authority="mission-42",
            )
            self.assertEqual(rival["reason"], "AUTHORITY_HELD")
            self.assertEqual(gate.view()["motion_authority"], "mission-42")
            port.release.set()
            thread.join(2)
            self.assertEqual(box["first"]["result"], "completed")
            self.assertIsNone(gate.view()["motion_authority"])

    def test_failure_and_stop_release_authority(self):
        port = FakePort()
        port.outcome = {"ok": False, "reason": "PATTERN_HTTP_FAILED", "error": "timeout"}
        with tempfile.TemporaryDirectory() as directory:
            gate = owner(directory, port)
            failed = gate.request("return_home", authority="manual")
            self.assertEqual(failed["result"], "failed")
            self.assertEqual(gate.view()["state"], "FAULT")
            self.assertEqual(gate.view()["fault_class"], "PATTERN_HTTP_FAILED")
            self.assertIsNone(gate.view()["motion_authority"])
            self.assertFalse(gate.view()["motion_permitted"])
            blocked = gate.request("return_home", authority="manual")
            self.assertEqual(blocked["reason"], "FAULT_NOT_CLEARED")
            cleared = gate.clear(operator="manual")
            self.assertEqual(cleared["result"], "cleared")
            self.assertEqual(gate.view()["state"], "IDLE")

            def interrupt(_skill):
                gate.stop(authority="manual")

            port.hook = interrupt
            port.outcome = {"ok": True, "reason": "PATTERN_FINISHED"}
            stopped = gate.request("run_pattern", authority="ai-operator", params={"pattern": "circle"})
            self.assertEqual(stopped["result"], "stopped")
            self.assertEqual(gate.view()["state"], "STOPPED")
            self.assertIsNone(gate.view()["motion_authority"])
            self.assertIn(("stop",), port.calls)
            again = gate.request("return_home", authority="manual")
            self.assertEqual(again["reason"], "STOPPED_NOT_CLEARED")

    def test_restart_during_moving_does_not_claim_motion(self):
        port = FakePort()
        with tempfile.TemporaryDirectory() as directory:
            gate = owner(directory, port)
            gate.store.transition(
                "PREPARING", now=NOW, trace_id="live", reason="SKILL_ACCEPTED",
                skill="move_to_pose", motion_authority="manual",
            )
            gate.store.transition(
                "MOVING", now=NOW, trace_id="live", reason="SKILL_STARTED",
                motion_authority="manual",
            )
            restarted = owner(directory, port)
            view = restarted.view()
            self.assertEqual(view["state"], "STOPPED")
            self.assertEqual(view["transition_reason"], "RECOVERY_UNPROVEN_MOTION")
            self.assertEqual(view["previous_state"], "MOVING")
            self.assertIsNone(view["motion_authority"])
            self.assertIsNone(view["skill"])
            self.assertFalse(view["motion_permitted"])

    def test_readiness_and_command_path(self):
        port = FakePort()
        with tempfile.TemporaryDirectory() as directory:
            cold = owner(directory, port, lambda: {"reachable": False, "fresh": False})
            rejected = cold.request("return_home", authority="manual")
            self.assertEqual(rejected["reason"], "ARM_NOT_READY")
            port.available_flag = False
            closed = owner(directory, port)
            self.assertEqual(
                closed.request("return_home", authority="manual")["reason"],
                "COMMAND_PATH_UNAVAILABLE",
            )

    def test_engineering_cannot_run_during_a_skill(self):
        port = FakePort()
        port.started = threading.Event()
        port.release = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            gate = owner(directory, port)

            def run():
                gate.request("move_to_pose", authority="manual", params={"x": 0, "y": 0, "z": 100})

            thread = threading.Thread(target=run)
            thread.start()
            self.assertTrue(port.started.wait(2))
            raw = gate.engineering({"T": 105}, authority="manual")
            self.assertEqual(raw["reason"], "AUTHORITY_HELD")
            self.assertFalse(any(call[0] == "engineering" for call in port.calls))
            port.release.set()
            thread.join(2)
            sent = gate.engineering({"T": 105}, authority="manual", trace_id="eng-1")
            self.assertEqual(sent["result"], "completed")
            self.assertEqual(port.calls[-1][0], "engineering")
            self.assertIsNone(gate.view()["motion_authority"])

    def test_stop_while_idle_does_not_trap_the_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            gate = owner(directory)
            result = gate.stop(authority="manual")
            self.assertEqual(result["reason"], "NOT_ACTIVE")
            self.assertEqual(gate.view()["state"], "IDLE")

    def test_http_skill_api_is_independent_of_the_page(self):
        port = FakePort()
        with tempfile.TemporaryDirectory() as directory:
            gate = owner(directory, port)
            server = load_server()
            handler = server.make_handler(Path(directory), ROOT / "ui", 120, 20, gate)
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                body = json.dumps({
                    "skill": "run_pattern",
                    "authority": "camera-event-17",
                    "params": {"pattern": "spiral"},
                }).encode("utf-8")
                request = urllib.request.Request(
                    "http://127.0.0.1:%d/api/roarm/skills" % httpd.server_port,
                    data=body,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(request, timeout=2) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload["result"], "completed")
                self.assertEqual(port.calls[0][2]["pattern"], "spiral")
            finally:
                httpd.shutdown()
                httpd.server_close()

    def test_guardian_reads_operational_state_without_writing_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operational-state.json"
            missing = load_published_operational(path)
            self.assertIsNone(missing["state"])
            self.assertFalse(path.exists())
            gate = owner(directory)
            gate.request("return_home", authority="manual", trace_id="kept")
            published = load_published_operational(path)
            self.assertEqual(published["state"], "IDLE")
            self.assertEqual(published["last_completed_action"], "return_home")
        source = (ROOT / "guardian" / "aggregator.py").read_text(encoding="utf-8")
        self.assertNotIn("operational.recover", source)
        self.assertIn("load_published_operational", source)

    def test_mission_control_renders_the_control_panel(self):
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn("RoArm control", html)
        self.assertIn("Move to pose", html)
        self.assertIn("Return/Home", html)
        self.assertIn(">STOP<", html)
        self.assertIn("Engineering / direct control", html)
        self.assertIn("lissajous", html)
        self.assertIn("no standalone gripper command", html)
        self.assertIn("Operational state", html)
        self.assertIn("Motion authority", script)
        self.assertIn('fetch("/api/roarm/skills"', script)
        self.assertIn('fetch("/api/roarm/engineering"', script)
        self.assertNotIn("confirm(", script)

    def test_production_port_requires_the_command_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            port = ProductionCommandPort(directory)
            self.assertFalse(port.available())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            self.assertEqual(readiness_from_snapshot(path), {"reachable": False, "fresh": False})
            path.write_text(json.dumps({
                "roarm": {"reachability": "reachable", "connected": True, "fresh": True, "t105_fresh": True}
            }), encoding="utf-8")
            self.assertEqual(readiness_from_snapshot(path)["fresh"], True)


def load_server():
    spec = importlib.util.spec_from_file_location("mc_server_skills", ROOT / "ui" / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    unittest.main()
