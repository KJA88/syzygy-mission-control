"""Offline Mission Engine, cockpit routes, and restart safety."""
import importlib.util
import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from missions.actions import SkillActionPort
from missions.definitions import MissionDefinition, load_definitions
from missions.engine import MissionEngine, open_engine
from missions.health import required_health
from missions.store import RunStore
from missions.triggers import perception_trigger

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += float(seconds)


class FakeHome:
    def __init__(self, entity_id="switch.1_plug_shelly", state="off", writable=True, available=True):
        self.entity_id = entity_id
        self.state = state
        self.writable = writable
        self.available = available
        self.view_available = True
        self.view_reason = None
        self.calls = []
        self.block_on = None
        self.release = None
        self.entered = threading.Event()
        self.fail_action = None
        self.reject_turn_offs = 0
        self.lag_off_reads = 0
        self._lag = 0
        self.hold_state = False

    def entity_view(self):
        if not self.view_available:
            return {"available": False, "reason": self.view_reason or "HA_UNAVAILABLE", "entities": []}
        reported = self.state
        if self._lag:
            reported = "on"
            self._lag -= 1
            if self._lag == 0:
                self.state = "off"
        capabilities = ["read", "turn_on", "turn_off"] if self.writable else ["read"]
        return {
            "available": True,
            "reason": None,
            "entities": [{
                "entity_id": self.entity_id,
                "state": reported,
                "available": self.available,
                "writable": self.writable,
                "capabilities": capabilities,
                "domain": self.entity_id.split(".", 1)[0],
            }],
        }

    def act(self, entity_id, action, brightness=None):
        self.calls.append((entity_id, action))
        if self.block_on == action and self.release is not None:
            self.entered.set()
            self.release.wait(2)
        if self.fail_action == action:
            return {"accepted": False, "reason": "POLICY_DENIED"}
        if action == "turn_off" and self.reject_turn_offs:
            self.reject_turn_offs -= 1
            return {"accepted": False, "reason": "POLICY_DENIED"}
        if action == "turn_off" and self.lag_off_reads:
            self._lag = self.lag_off_reads
        elif not self.hold_state:
            if action == "turn_on":
                self.state = "on"
            elif action == "turn_off":
                self.state = "off"
        return {"accepted": True, "reason": "ACTION_ACCEPTED", "entity_id": entity_id, "action": action}


def definition(target="switch.1_plug_shelly", wait_s=0.2, observe=0.2):
    return MissionDefinition({
        "id": "shelly_plug_cycle",
        "name": "Shelly plug cycle",
        "version": 1,
        "kind": "switch_cycle",
        "physical": True,
        "subsystem": "home_assistant",
        "target": target,
        "final_state": "off",
        "wait_s": wait_s,
        "observe_timeout_s": observe,
    })


def engine(home, health=None, before_step=None, synchronous=True, store=None, definitions=None):
    clock = Clock()
    if store is None:
        directory = tempfile.mkdtemp()
        store = RunStore(Path(directory) / "mission-runs.json")
    return MissionEngine(
        definitions or {"shelly_plug_cycle": definition()},
        store,
        home,
        health or (lambda: (True, None)),
        clock=clock,
        sleep=clock.sleep,
        synchronous=synchronous,
        before_step=before_step,
    )


class MissionTests(unittest.TestCase):
    def test_operator_cycle_turns_on_waits_and_restores_off(self):
        home = FakeHome()
        mission = engine(home)
        started = mission.start("shelly_plug_cycle", operator="kja")
        self.assertTrue(started["accepted"])
        self.assertEqual(started["state"], "COMPLETE")
        run = mission.history(5)[0]
        self.assertEqual(run["state"], "COMPLETE")
        self.assertEqual(run["final_state"], "off")
        self.assertEqual(run["trigger"]["source"], "operator")
        self.assertEqual(run["trigger"]["metadata"]["operator"], "kja")
        self.assertEqual([(item["action"], item["phase"], item["observed_state"]) for item in run["actions"]], [
            ("turn_on", "normal", "on"),
            ("turn_off", "normal", "off"),
        ])
        self.assertEqual(home.calls, [("switch.1_plug_shelly", "turn_on"), ("switch.1_plug_shelly", "turn_off")])
        self.assertTrue(all(item["outcome"] == "pass" for item in run["checks"]))
        for field in ("run_id", "mission_id", "version", "started_at", "updated_at", "completed_at", "checks", "actions"):
            self.assertIn(field, run)
        self.assertEqual(mission.status()["state"], "IDLE")
        self.assertTrue(mission.health_summary()["available"])
        self.assertFalse(run["energized"])

    def test_second_physical_mission_is_rejected_while_one_owns_the_engine(self):
        home = FakeHome()
        home.block_on = "turn_on"
        home.release = threading.Event()
        mission = engine(home, synchronous=False)
        first = mission.start("shelly_plug_cycle")
        self.assertTrue(home.entered.wait(2))
        second = mission.start("shelly_plug_cycle")
        self.assertFalse(second["accepted"])
        self.assertEqual(second["reason"], "OWNER_BUSY")
        home.release.set()
        mission.join(2)
        self.assertEqual(mission.history(5)[0]["state"], "COMPLETE")
        self.assertEqual(len([run for run in mission.history(5) if run["run_id"] == first["run_id"]]), 1)

    def test_unknown_and_malformed_requests_do_not_act(self):
        home = FakeHome()
        mission = engine(home)
        unknown = mission.start("missing")
        self.assertEqual(unknown["reason"], "UNKNOWN_MISSION")
        malformed = mission.start(None)
        self.assertEqual(malformed["reason"], "MALFORMED_PARAMETERS")
        perception = mission.start("shelly_plug_cycle", trigger=perception_trigger({
            "camera": "frontyard", "class": "person", "confidence": 0.9,
        }))
        self.assertEqual(perception["reason"], "TRIGGER_NOT_ENABLED")
        self.assertEqual(home.calls, [])
        self.assertEqual(perception_trigger({"class": "car"})["source"], "perception")
        self.assertIsNone(perception_trigger({"class": "person"})["entity_id"])

    def test_precheck_failures_happen_before_any_action(self):
        cases = []
        unhealthy = FakeHome()
        cases.append((engine(unhealthy, health=lambda: (False, "SYSTEM_UNHEALTHY")), "SYSTEM_UNHEALTHY"))
        down = FakeHome()
        down.view_available = False
        cases.append((engine(down), "HA_UNAVAILABLE"))
        missing = FakeHome(entity_id="switch.other")
        cases.append((engine(missing), "UNKNOWN_ENTITY"))
        offline = FakeHome(available=False, state="unavailable")
        cases.append((engine(offline), "TARGET_UNAVAILABLE"))
        locked = FakeHome(writable=False)
        cases.append((engine(locked), "TARGET_NOT_WRITABLE"))
        unknown = FakeHome(state="unknown")
        cases.append((engine(unknown), "START_STATE_UNKNOWN"))
        for mission, reason in cases:
            with self.subTest(reason=reason):
                result = mission.start("shelly_plug_cycle")
                self.assertTrue(result["accepted"])
                run = mission.history(1)[0]
                self.assertEqual(run["state"], "FAULT")
                self.assertEqual(run["reason"], reason)
                self.assertEqual(run["actions"], [])

    def test_policy_rejection_is_not_bypassed(self):
        home = FakeHome()
        home.fail_action = "turn_on"
        mission = engine(home)
        mission.start("shelly_plug_cycle")
        run = mission.history(1)[0]
        self.assertEqual(run["state"], "FAULT")
        self.assertEqual(run["reason"], "POLICY_DENIED")
        self.assertEqual([item["phase"] for item in run["actions"]], ["normal"])
        self.assertEqual(home.calls, [("switch.1_plug_shelly", "turn_on")])

    def test_observed_state_mismatch_faults_and_attempts_cleanup(self):
        home = FakeHome()
        home.hold_state = True
        mission = engine(home)
        mission.start("shelly_plug_cycle")
        run = mission.history(1)[0]
        self.assertEqual(run["state"], "FAULT")
        self.assertEqual(run["reason"], "ACTION_STATE_MISMATCH")
        self.assertEqual([item["phase"] for item in run["actions"]], ["normal", "cleanup"])
        self.assertEqual(run["actions"][0]["observed_state"], "off")

    def test_stop_before_the_first_action_performs_no_action(self):
        home = FakeHome()

        def before(run):
            if run["step"] == "checks":
                mission.stop()

        mission = engine(home, before_step=before)
        mission.start("shelly_plug_cycle")
        run = mission.history(1)[0]
        self.assertEqual(run["state"], "STOPPED")
        self.assertEqual(run["reason"], "STOP_REQUESTED")
        self.assertEqual(run["actions"], [])
        self.assertEqual(home.calls, [])

    def test_stop_during_wait_skips_normal_steps_and_runs_cleanup(self):
        home = FakeHome()

        def before(run):
            if run["step"] == "wait":
                mission.stop()

        mission = engine(home, before_step=before)
        mission.start("shelly_plug_cycle")
        run = mission.history(1)[0]
        self.assertEqual(run["state"], "STOPPED")
        self.assertEqual(run["reason"], "STOP_REQUESTED")
        self.assertEqual([(item["action"], item["phase"]) for item in run["actions"]], [
            ("turn_on", "normal"),
            ("turn_off", "cleanup"),
        ])
        self.assertEqual(home.state, "off")
        self.assertFalse(run["energized"])
        self.assertTrue(run["cleanup"]["accepted"])
        self.assertTrue(run["cleanup"]["verified"])
        self.assertEqual([item["phase"] for item in run["actions"]].count("cleanup"), 1)
        self.assertNotIn("wait", [item["action"] for item in run["actions"]])

    def test_stop_during_an_inflight_action_does_not_run_later_normal_steps(self):
        home = FakeHome()
        home.block_on = "turn_on"
        home.release = threading.Event()
        mission = engine(home, synchronous=False)
        mission.start("shelly_plug_cycle")
        self.assertTrue(home.entered.wait(2))
        stopped = mission.stop()
        self.assertTrue(stopped["accepted"])
        home.release.set()
        mission.join(2)
        run = mission.history(1)[0]
        self.assertEqual(run["state"], "STOPPED")
        self.assertEqual([item["phase"] for item in run["actions"]], ["normal", "cleanup"])
        self.assertNotIn("turn_off", [item["action"] for item in run["actions"] if item["phase"] == "normal"])

    def test_cleanup_waits_for_a_lagging_off_readback(self):
        home = FakeHome()
        home.lag_off_reads = 1

        def before(run):
            if run["step"] == "wait":
                mission.stop()

        mission = engine(home, before_step=before)
        mission.start("shelly_plug_cycle")
        run = mission.history(1)[0]
        cleanup = run["actions"][-1]
        self.assertEqual(cleanup["phase"], "cleanup")
        self.assertEqual(cleanup["observed_state"], "on")
        self.assertTrue(run["cleanup"]["accepted"])
        self.assertTrue(run["cleanup"]["verified"])
        self.assertEqual(run["state"], "STOPPED")
        self.assertEqual(run["reason"], "STOP_REQUESTED")
        self.assertFalse(run["energized"])
        self.assertEqual([item[1] for item in home.calls], ["turn_on", "turn_off"])

    def test_rejected_turn_off_gets_one_cleanup_and_keeps_the_fault(self):
        home = FakeHome()
        home.reject_turn_offs = 1
        mission = engine(home)
        mission.start("shelly_plug_cycle")
        run = mission.history(1)[0]
        self.assertEqual(run["state"], "FAULT")
        self.assertEqual(run["reason"], "POLICY_DENIED")
        self.assertEqual([(item["action"], item["phase"], item["accepted"]) for item in run["actions"]], [
            ("turn_on", "normal", True),
            ("turn_off", "normal", False),
            ("turn_off", "cleanup", True),
        ])
        self.assertEqual(run["cleanup"]["reason"], "ACTION_ACCEPTED")
        self.assertTrue(run["cleanup"]["verified"])
        self.assertFalse(run["energized"])
        self.assertEqual([item[1] for item in home.calls], ["turn_on", "turn_off", "turn_off"])

    def test_switch_cycle_rejects_final_state_on_before_any_action(self):
        directory = tempfile.mkdtemp()
        path = Path(directory) / "missions.yaml"
        path.write_text(
            "missions:\n"
            "  - id: shelly_plug_cycle\n"
            "    name: Shelly plug cycle\n"
            "    version: 1\n"
            "    kind: switch_cycle\n"
            "    physical: true\n"
            "    subsystem: home_assistant\n"
            "    target: switch.1_plug_shelly\n"
            "    final_state: \"on\"\n"
            "    wait_s: 1\n"
            "    observe_timeout_s: 1\n",
            encoding="utf-8",
        )
        home = FakeHome()
        mission = open_engine(path, home, lambda: (True, None), Path(directory) / "runs.json")
        self.assertFalse(mission.status()["available"])
        self.assertEqual(mission.start("shelly_plug_cycle")["reason"], "MISSION_DEFINITION_INVALID")
        self.assertEqual(home.calls, [])

    def test_bridge_loss_during_the_mission_is_a_bounded_fault(self):
        home = FakeHome()
        original = home.entity_view

        def entity_view():
            if home.calls:
                home.view_available = False
            return original()

        home.entity_view = entity_view
        mission = engine(home)
        mission.start("shelly_plug_cycle")
        run = mission.history(1)[0]
        self.assertEqual(run["state"], "FAULT")
        self.assertEqual(run["reason"], "HA_UNAVAILABLE")
        self.assertLessEqual(len(home.calls), 2)

    def test_restart_marks_an_active_run_interrupted_and_does_not_replay_it(self):
        directory = tempfile.mkdtemp()
        path = Path(directory) / "mission-runs.json"
        stale = {
            "run_id": "stale",
            "mission_id": "shelly_plug_cycle",
            "state": "RUNNING",
            "step": "wait",
            "actions": [{"action": "turn_on", "target": "switch.1_plug_shelly", "phase": "normal"}],
            "checks": [],
            "reason": None,
        }
        path.write_text(json.dumps({"runs": [stale]}), encoding="utf-8")
        home = FakeHome()
        store = RunStore(path)
        self.assertEqual(store.runs[0]["state"], "FAULT")
        self.assertEqual(store.runs[0]["reason"], "INTERRUPTED")
        self.assertEqual(home.calls, [])
        mission = engine(home, store=store)
        self.assertEqual(home.calls, [])
        mission.start("shelly_plug_cycle")
        self.assertEqual(home.calls, [("switch.1_plug_shelly", "turn_on"), ("switch.1_plug_shelly", "turn_off")])
        self.assertEqual(mission.history(5)[1]["run_id"], "stale")
        self.assertEqual(mission.history(5)[1]["actions"], stale["actions"])
        self.assertNotEqual(mission.history(5)[0]["run_id"], "stale")

    def test_definition_target_is_used_instead_of_trigger_metadata(self):
        home = FakeHome("switch.other_plug")
        mission = engine(home, definitions={"shelly_plug_cycle": definition("switch.other_plug")})
        mission.start("shelly_plug_cycle", trigger={
            "source": "operator",
            "event_type": "start",
            "metadata": {"entity_id": "switch.1_plug_shelly"},
        })
        self.assertEqual({item[0] for item in home.calls}, {"switch.other_plug"})
        self.assertIsNone(mission.history(1)[0]["trigger"]["entity_id"])

    def test_invalid_definition_disables_starts_without_action(self):
        directory = tempfile.mkdtemp()
        path = Path(directory) / "missions.yaml"
        path.write_text("missions:\n  - id: bad\n    call_service: switch.turn_on\n", encoding="utf-8")
        home = FakeHome()
        mission = open_engine(path, home, lambda: (True, None), Path(directory) / "runs.json")
        self.assertFalse(mission.status()["available"])
        self.assertEqual(mission.start("bad")["reason"], "MISSION_DEFINITION_INVALID")
        self.assertEqual(home.calls, [])

    def test_repository_mission_is_the_shelly_cycle(self):
        loaded = load_definitions(ROOT / "config" / "missions.yaml")
        self.assertEqual(list(loaded), ["shelly_plug_cycle"])
        mission = loaded["shelly_plug_cycle"]
        self.assertEqual(mission.target, "switch.1_plug_shelly")
        self.assertEqual(mission.final_state, "off")
        self.assertEqual(mission.kind, "switch_cycle")
        text = (ROOT / "config" / "missions.yaml").read_text(encoding="utf-8")
        self.assertNotIn("call_service", text)
        for source in (ROOT / "missions").glob("*.py"):
            body = source.read_text(encoding="utf-8")
            self.assertNotIn("call_service(", body)
            self.assertNotIn("/api/services/", body)
            self.assertNotIn("urllib.request", body)
            self.assertNotIn("operational-state", body)
            self.assertNotIn("T105", body)
            self.assertNotIn("T100", body)

    def test_skill_adapter_reaches_only_the_phase3_owner(self):
        seen = {}

        class Owner:
            def request(self, skill, authority=None, operator=None, mission=None, trace_id=None, params=None):
                seen["skill"] = skill
                seen["params"] = params
                return {"accepted": True, "result": "accepted", "reason": "SKILL_ACCEPTED"}

        port = SkillActionPort(Owner())
        self.assertEqual(port.request("explode")["reason"], "UNKNOWN_SKILL")
        self.assertEqual(seen, {})
        self.assertEqual(port.request("stop", mission_id="run-1")["reason"], "SKILL_ACCEPTED")
        self.assertEqual(seen["skill"], "stop")
        self.assertNotIn("SkillActionPort", (ROOT / "missions" / "engine.py").read_text(encoding="utf-8"))

    def test_required_health_ignores_optional_degradation(self):
        self.assertEqual(required_health({"system": {"status": "unknown"}}), (False, "SYSTEM_UNKNOWN"))
        self.assertEqual(required_health({"system": {"status": "red"}, "services": []}), (False, "SYSTEM_UNHEALTHY"))
        yellow = {
            "system": {"status": "yellow"},
            "services": [
                {"id": "mission-control", "required": True, "status": "green"},
                {"id": "home-assistant-mcp", "required": False, "status": "red"},
            ],
        }
        self.assertEqual(required_health(yellow), (True, None))
        yellow["services"][0]["status"] = "red"
        self.assertEqual(required_health(yellow), (False, "SYSTEM_UNHEALTHY"))

    def test_cockpit_routes_keep_home_perception_and_roarm_paths(self):
        page = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="view-missions"', page)
        self.assertIn('id="mission-stop"', page)
        self.assertIn('id="operational"', page)
        self.assertIn('id="view-home"', page)
        self.assertIn("/api/missions/start", script)
        self.assertIn("/api/perception/cameras", script)
        self.assertIn("/api/roarm/skills", script)
        self.assertIn("/api/home/entities", script)
        self.assertNotIn("HA_TOKEN", script)
        self.assertNotIn("call_service", script)
        self.assertIn("missionStop.addEventListener", script)
        spec = importlib.util.spec_from_file_location("mc_mission_server", ROOT / "ui" / "server.py")
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)
        home = FakeHome()
        mission = engine(home)
        handler = server.make_handler(
            ROOT / "state", ROOT / "ui", 120, 20, owner=None, vision=_Vision(), home=home, missions=mission)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % httpd.server_port
        try:
            started = _post(base + "/api/missions/start", {"mission": "shelly_plug_cycle"})
            self.assertEqual(started["state"], "COMPLETE")
            rejected = _post(base + "/api/missions/start", {"mission": "shelly_plug_cycle", "action": "turn_on"})
            self.assertEqual(rejected["reason"], "MALFORMED_PARAMETERS")
            idle = _post(base + "/api/missions/stop", {})
            self.assertEqual(idle["reason"], "NO_ACTIVE_MISSION")
            with urllib.request.urlopen(base + "/api/missions", timeout=2) as response:
                catalog = json.loads(response.read().decode("utf-8"))
            self.assertEqual(catalog["missions"][0]["target"], "switch.1_plug_shelly")
            with urllib.request.urlopen(base + "/api/home/entities", timeout=2) as response:
                entities = json.loads(response.read().decode("utf-8"))
            self.assertTrue(entities["available"])
            with urllib.request.urlopen(base + "/api/perception/cameras", timeout=2) as response:
                cameras = json.loads(response.read().decode("utf-8"))
            self.assertEqual(cameras["cameras"][0]["id"], "frontyard")
            with urllib.request.urlopen(base + "/api/health", timeout=2) as response:
                health = json.loads(response.read().decode("utf-8"))
            self.assertTrue(health["ok"])
            self.assertTrue(health["mission_engine"]["available"])
            self.assertEqual(health["mission_engine"]["state"], "IDLE")
            self.assertEqual(home.state, "off")
        finally:
            httpd.shutdown()
            httpd.server_close()


class _Vision:
    def camera_view(self):
        return {"available": True, "cameras": [{"id": "frontyard", "name": "Front"}]}


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
