import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from guardian.aggregator import build_snapshot
from guardian.config import load_config
from guardian.model import stamp
from guardian.state_engine import (
    LEGAL_TRANSITIONS,
    OPERATIONAL_STATES,
    OperationalStateStore,
    apply_transition,
    attach_operational_state,
    build_state,
)
from guardian.storage import atomic_json, read_snapshot

NOW = 1800000000
ROOT = Path(__file__).resolve().parents[1]


def drive(steps):
    store_record = {
        "schema_version": 1, "role": "authoritative", "state": "IDLE",
        "previous_state": None, "transition_at": stamp(NOW),
        "transition_reason": "INITIAL", "operator": None, "skill": None,
        "mission": None, "motion_permitted": False, "motion_authority": None,
        "fault_class": None, "fault_reason": None, "last_known_good": "IDLE",
        "last_completed_action": None, "trace_id": "origin",
        "freshness": "fresh", "last_rejection": None,
    }
    for target, extra in steps:
        store_record, accepted = apply_transition(
            store_record, target, now=NOW, trace_id="trace", reason="step", **extra)
        if not accepted:
            raise AssertionError(target)
    return store_record


PATHS = {
    "IDLE": [],
    "PREPARING": [("PREPARING", {
        "operator": "operator-a", "skill": "reach", "mission": "mission-1",
        "motion_authority": "owner-a"})],
    "MOVING": [("PREPARING", {
        "operator": "operator-a", "skill": "reach", "mission": "mission-1",
        "motion_authority": "owner-a"}), ("MOVING", {})],
    "COMPLETE": [("PREPARING", {
        "operator": "operator-a", "skill": "reach", "mission": "mission-1",
        "motion_authority": "owner-a"}), ("MOVING", {}), ("COMPLETE", {})],
    "FAULT": [("FAULT", {"fault_class": "TEST_FAULT", "fault_reason": "broken"})],
    "STOPPED": [("STOPPED", {})],
}


def extras_for(target):
    if target == "FAULT":
        return {"fault_class": "TEST_FAULT", "fault_reason": "broken"}
    if target == "PREPARING":
        return {"operator": "operator-a", "skill": "reach", "mission": "mission-1",
                "motion_authority": "owner-a"}
    if target == "MOVING":
        return {"motion_authority": "owner-a"}
    return {}


class OperationalTransitionTests(unittest.TestCase):
    def test_every_legal_transition(self):
        seen = set()
        for source, targets in LEGAL_TRANSITIONS.items():
            for target in targets:
                record = drive(PATHS[source])
                self.assertEqual(record["state"], source)
                nxt, accepted = apply_transition(
                    record, target, now=NOW + 1, trace_id="legal",
                    reason="allowed", **extras_for(target))
                self.assertTrue(accepted, (source, target))
                self.assertEqual(nxt["state"], target)
                self.assertEqual(nxt["previous_state"], source)
                self.assertEqual(nxt["transition_reason"], "allowed")
                self.assertEqual(nxt["trace_id"], "legal")
                seen.add((source, target))
        self.assertEqual(len(seen), sum(len(v) for v in LEGAL_TRANSITIONS.values()))

    def test_illegal_transitions_are_rejected_and_recorded(self):
        for source in OPERATIONAL_STATES:
            record = drive(PATHS[source])
            illegal = [name for name in OPERATIONAL_STATES if name not in LEGAL_TRANSITIONS[source]]
            self.assertIn(source, illegal)
            for target in illegal:
                nxt, accepted = apply_transition(
                    record, target, now=NOW + 2, trace_id="reject",
                    reason="nope", fault_class="TEST_FAULT",
                    motion_authority="owner-a")
                self.assertFalse(accepted)
                self.assertEqual(nxt["state"], source)
                self.assertEqual(nxt["last_rejection"]["reason"], "ILLEGAL_TRANSITION")
                self.assertEqual(nxt["last_rejection"]["from_state"], source)
                self.assertEqual(nxt["last_rejection"]["to_state"], target)
                self.assertEqual(nxt["trace_id"], record["trace_id"])

    def test_fault_requires_a_class_and_clears_motion_permission(self):
        idle = drive([])
        rejected, accepted = apply_transition(
            idle, "FAULT", now=NOW, trace_id="t", reason="fault")
        self.assertFalse(accepted)
        self.assertEqual(rejected["last_rejection"]["reason"], "FAULT_DETAIL_REQUIRED")
        self.assertEqual(rejected["state"], "IDLE")
        faulted, accepted = apply_transition(
            idle, "FAULT", now=NOW, trace_id="t", reason="fault",
            fault_class="LINK_LOST", fault_reason="no route")
        self.assertTrue(accepted)
        self.assertEqual(faulted["state"], "FAULT")
        self.assertEqual(faulted["fault_class"], "LINK_LOST")
        self.assertEqual(faulted["fault_reason"], "no route")
        self.assertFalse(faulted["motion_permitted"])
        self.assertIsNone(faulted["motion_authority"])
        self.assertEqual(faulted["last_known_good"], "IDLE")

    def test_stopped_releases_authority_without_claiming_completion(self):
        moving = drive(PATHS["MOVING"])
        stopped, accepted = apply_transition(
            moving, "STOPPED", now=NOW, trace_id="stop", reason="operator stop")
        self.assertTrue(accepted)
        self.assertEqual(stopped["state"], "STOPPED")
        self.assertFalse(stopped["motion_permitted"])
        self.assertIsNone(stopped["motion_authority"])
        self.assertIsNone(stopped["skill"])
        self.assertIsNone(stopped["last_completed_action"])
        self.assertEqual(stopped["last_known_good"], "IDLE")

    def test_last_known_good_follows_idle_and_complete_only(self):
        complete = drive(PATHS["COMPLETE"])
        self.assertEqual(complete["last_known_good"], "COMPLETE")
        self.assertEqual(complete["last_completed_action"], "reach")
        faulted, accepted = apply_transition(
            complete, "FAULT", now=NOW, trace_id="t", reason="later",
            fault_class="TEST_FAULT", fault_reason="after")
        self.assertTrue(accepted)
        self.assertEqual(faulted["last_known_good"], "COMPLETE")
        self.assertEqual(faulted["last_completed_action"], "reach")
        idle, accepted = apply_transition(
            faulted, "IDLE", now=NOW, trace_id="t", reason="cleared")
        self.assertTrue(accepted)
        self.assertEqual(idle["last_known_good"], "IDLE")
        self.assertEqual(idle["last_completed_action"], "reach")
        self.assertIsNone(idle["fault_class"])

    def test_authority_owner_changes_only_through_an_accepted_transition(self):
        preparing = drive(PATHS["PREPARING"])
        self.assertEqual(preparing["motion_authority"], "owner-a")
        self.assertEqual(preparing["operator"], "operator-a")
        self.assertTrue(preparing["motion_permitted"])
        blocked, accepted = apply_transition(
            drive([("PREPARING", {"skill": "reach"})]),
            "MOVING", now=NOW, trace_id="t", reason="go")
        self.assertFalse(accepted)
        self.assertEqual(blocked["state"], "PREPARING")
        self.assertEqual(blocked["last_rejection"]["reason"], "AUTHORITY_REQUIRED")
        self.assertFalse(blocked["motion_permitted"])
        moved, accepted = apply_transition(
            preparing, "MOVING", now=NOW, trace_id="t", reason="go",
            motion_authority="owner-b", operator="operator-b")
        self.assertTrue(accepted)
        self.assertEqual(moved["motion_authority"], "owner-b")
        self.assertEqual(moved["operator"], "operator-b")
        self.assertTrue(moved["motion_permitted"])


class OperationalRecoveryTests(unittest.TestCase):
    def test_missing_file_recovers_to_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OperationalStateStore(Path(directory) / "operational-state.json")
            record = store.recover(NOW, "boot")
            self.assertEqual(record["state"], "IDLE")
            self.assertEqual(record["transition_reason"], "INITIAL")
            self.assertFalse(record["motion_permitted"])
            again = OperationalStateStore(store.path).recover(NOW + 1, "second")
            self.assertEqual(again["state"], "IDLE")
            self.assertEqual(again["transition_reason"], "INITIAL")

    def test_idle_restart_keeps_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operational-state.json"
            first = OperationalStateStore(path)
            first.recover(NOW, "boot")
            second = OperationalStateStore(path).recover(NOW + 5, "reboot")
            self.assertEqual(second["state"], "IDLE")
            self.assertNotEqual(second["transition_reason"], "RECOVERY_UNPROVEN_MOTION")
            self.assertFalse(second["motion_permitted"])

    def test_moving_and_preparing_restart_as_stopped(self):
        for active in ("MOVING", "PREPARING"):
            with self.subTest(active=active):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "operational-state.json"
                    atomic_json(path, drive(PATHS[active]))
                    recovered = OperationalStateStore(path).recover(NOW + 9, "reboot")
                    self.assertEqual(recovered["state"], "STOPPED")
                    self.assertEqual(recovered["previous_state"], active)
                    self.assertEqual(recovered["transition_reason"], "RECOVERY_UNPROVEN_MOTION")
                    self.assertFalse(recovered["motion_permitted"])
                    self.assertIsNone(recovered["motion_authority"])
                    self.assertIsNone(recovered["skill"])
                    self.assertEqual(recovered["last_known_good"], "IDLE")

    def test_invalid_record_recovers_to_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operational-state.json"
            path.write_text("{", encoding="utf-8")
            recovered = OperationalStateStore(path).recover(NOW, "boot")
            self.assertEqual(recovered["state"], "IDLE")
            self.assertEqual(recovered["transition_reason"], "RECOVERY_INVALID")
            self.assertFalse(recovered["motion_permitted"])

    def test_publication_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operational-state.json"
            store = OperationalStateStore(path)
            store.recover(NOW, "boot")
            replaced = {}
            real_replace = __import__("os").replace
            real_fsync = __import__("os").fsync

            def spy_replace(src, dst):
                replaced["temp"] = Path(src).read_text(encoding="utf-8")
                replaced["dest_before"] = Path(dst).read_text(encoding="utf-8")
                return real_replace(src, dst)

            def spy_fsync(fd):
                replaced["fsynced"] = True
                return real_fsync(fd)

            with patch("guardian.storage.os.replace", side_effect=spy_replace), patch(
                "guardian.storage.os.fsync", side_effect=spy_fsync):
                view, accepted = store.transition(
                    "STOPPED", now=NOW + 1, trace_id="persist", reason="stop")
            self.assertTrue(accepted)
            self.assertTrue(replaced["fsynced"])
            self.assertEqual(json.loads(replaced["dest_before"])["state"], "IDLE")
            self.assertEqual(json.loads(replaced["temp"])["state"], "STOPPED")
            self.assertEqual(view["state"], "STOPPED")
            self.assertEqual(list(Path(directory).glob(".operational-state.json*")), [])


class OperationalEvidenceTests(unittest.TestCase):
    def test_stale_snapshot_does_not_present_active_motion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            moving = drive(PATHS["MOVING"])
            idle = drive([])
            atomic_json(path, {
                "guardian": {"heartbeat_at": stamp(NOW), "last_run_result": "ok"},
                "system": {"status": "green"},
                "state_engine": {"entities": [], "operational": moving},
            })
            stale = read_snapshot(path, NOW + 121)
            operational = stale["state_engine"]["operational"]
            self.assertIsNone(operational["state"])
            self.assertEqual(operational["recorded_state"], "MOVING")
            self.assertFalse(operational["motion_permitted"])
            self.assertEqual(operational["freshness"], "stale")
            self.assertEqual(operational["display_reason"], "UNPROVEN_ACTIVE_STATE")
            atomic_json(path, {
                "guardian": {"heartbeat_at": stamp(NOW), "last_run_result": "ok"},
                "system": {"status": "green"},
                "state_engine": {"entities": [], "operational": idle},
            })
            remembered = read_snapshot(path, NOW + 121)["state_engine"]["operational"]
            self.assertEqual(remembered["state"], "IDLE")
            self.assertEqual(remembered["freshness"], "stale")
            self.assertFalse(remembered["motion_permitted"])

    def test_authoritative_state_does_not_change_guardian_rollup_or_evidence(self):
        cfg = load_config("config/services.yaml")
        snapshot = build_snapshot(cfg, {}, {}, NOW, "trace")
        before = snapshot["system"]["status"]
        evidence = build_state(snapshot)
        mission = next(
            item for item in evidence["entities"] if item["id"] == "system/syzygy")
        self.assertEqual(mission["attributes"]["current_mission"]["knowledge"], "unknown")
        self.assertIsNone(mission["attributes"]["current_mission"]["value"])
        attach_operational_state(snapshot, drive(PATHS["FAULT"]))
        self.assertEqual(snapshot["system"]["status"], before)
        self.assertEqual(snapshot["state_engine"]["operational"]["state"], "FAULT")
        self.assertNotIn("operational", evidence)

    def test_mission_control_renders_the_operational_card(self):
        page = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        source = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="operational"', page)
        for label in (
            "Operational state",
            "Active operator",
            "Active skill/action",
            "Active mission",
            "Motion permitted",
            "Motion authority",
            "Last transition",
            "Fault reason",
            "Last completed action",
        ):
            self.assertIn(label, source)
        self.assertIn('known(op && op.state)', source)
        spec = importlib.util.spec_from_file_location("mc_ui_server", ROOT / "ui" / "server.py")
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)
        shown = server.apply_heartbeat_freshness({
            "guardian": {"heartbeat_at": stamp(NOW), "last_run_result": "ok"},
            "system": {"status": "green"},
            "state_engine": {"operational": drive(PATHS["PREPARING"])},
        }, NOW + 121, hard_stale=120)
        self.assertIsNone(shown["state_engine"]["operational"]["state"])
        self.assertFalse(shown["state_engine"]["operational"]["motion_permitted"])


if __name__ == "__main__":
    unittest.main()
