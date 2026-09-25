"""Minimal tests for Mission Control UI server freshness + events tail."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "ui" / "server.py"
FIX = Path(__file__).resolve().parent / "fixtures"


def load_server():
    spec = importlib.util.spec_from_file_location("mc_ui_server", SERVER)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class FreshnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_server()

    def test_fresh_heartbeat_preserves_system_status(self):
        snap = json.loads((FIX / "snapshot.json").read_text(encoding="utf-8"))
        hb = snap["guardian"]["heartbeat_at"]
        now = datetime.fromisoformat(hb.replace("Z", "+00:00")).timestamp() + 5
        out = self.mod.apply_heartbeat_freshness(snap, now, hard_stale=120)
        self.assertEqual(out["system"]["status"], "green")
        self.assertEqual(out["guardian"]["status"], "green")
        self.assertAlmostEqual(out["guardian"]["observation_age_s"], 5, delta=0.5)

    def test_stale_heartbeat_forces_unknown(self):
        snap = json.loads((FIX / "snapshot_stale.json").read_text(encoding="utf-8"))
        snap["state_engine"] = {"entities": [
            {"id": "system/syzygy", "attributes": {
                "health": {"value": "green", "knowledge": "derived",
                           "freshness": "fresh", "confidence": 1.0,
                           "source": "guardian/reducer", "trace_id": "trace-1"}}},
            {"id": "node/pi", "attributes": {
                "health": {"value": "green", "knowledge": "derived",
                           "freshness": "fresh", "source": "guardian/node/pi",
                           "trace_id": "node-trace"},
                "host": {"value": "pi", "knowledge": "configured",
                         "freshness": "fresh"},
                "requested_input": {
                    "value": "HDMI 2", "knowledge": "requested",
                    "freshness": "fresh", "source": "operator/request",
                    "observed_at": "2026-09-24T20:00:00Z",
                    "trace_id": "request-trace", "confidence": 1.0,
                    "reason": "OPERATOR_REQUEST"},
                "remembered_input": {
                    "value": "HDMI 1", "knowledge": "remembered",
                    "freshness": "fresh", "source": "state/cache",
                    "observed_at": "2026-09-24T19:59:30Z",
                    "trace_id": "memory-trace", "confidence": 0.6,
                    "reason": "LAST_KNOWN"},
                "unknown_input": {
                    "value": None, "knowledge": "unknown",
                    "freshness": "unknown", "source": "tv-state-adapter",
                    "observed_at": None, "trace_id": "unknown-trace",
                    "confidence": None, "reason": "EVIDENCE_MISSING"}}},
        ]}
        # File claims green; reader must force unknown when age > 120s.
        now = datetime.now(timezone.utc).timestamp()
        out = self.mod.apply_heartbeat_freshness(snap, now, hard_stale=120)
        self.assertEqual(out["system"]["status"], "unknown")
        self.assertEqual(out["system"]["reason"], "GUARDIAN_HEARTBEAT_STALE")
        self.assertEqual(out["guardian"]["status"], "unknown")
        self.assertEqual(out["guardian"]["class"], "SNAPSHOT_STALE")
        entities = {item["id"]: item for item in out["state_engine"]["entities"]}
        system_health = entities["system/syzygy"]["attributes"]["health"]
        self.assertEqual(system_health["knowledge"], "unknown")
        self.assertIsNone(system_health["value"])
        self.assertEqual(system_health["freshness"], "stale")
        node = entities["node/pi"]["attributes"]
        self.assertEqual(node["health"]["freshness"], "stale")
        self.assertEqual(node["health"]["trace_id"], "node-trace")
        self.assertEqual(node["host"]["freshness"], "fresh")
        self.assertEqual(node["requested_input"], {
            "value": "HDMI 2", "knowledge": "requested",
            "freshness": "stale", "source": "operator/request",
            "observed_at": "2026-09-24T20:00:00Z",
            "trace_id": "request-trace", "confidence": 1.0,
            "reason": "OPERATOR_REQUEST"})
        self.assertEqual(node["remembered_input"], {
            "value": "HDMI 1", "knowledge": "remembered",
            "freshness": "stale", "source": "state/cache",
            "observed_at": "2026-09-24T19:59:30Z",
            "trace_id": "memory-trace", "confidence": 0.6,
            "reason": "LAST_KNOWN"})
        self.assertEqual(node["unknown_input"]["freshness"], "unknown")

    def test_missing_heartbeat_forces_unknown(self):
        snap = {"guardian": {"status": "green"}, "system": {"status": "green"}}
        out = self.mod.apply_heartbeat_freshness(snap, self.mod.utcnow(), 120)
        self.assertEqual(out["system"]["status"], "unknown")
        self.assertEqual(out["system"]["reason"], "GUARDIAN_HEARTBEAT_STALE")

    def test_read_snapshot_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            out = self.mod.read_snapshot_file(path, self.mod.utcnow(), 120)
            self.assertEqual(out["system"]["status"], "unknown")
            self.assertEqual(out["system"]["reason"], "SNAPSHOT_MISSING")


class EventsTailTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_server()

    def test_tail_limit(self):
        events = self.mod.tail_jsonl(FIX / "events.jsonl", limit=5)
        self.assertEqual(len(events), 5)
        self.assertEqual(events[-1]["message"], "pad event 24")

    def test_tail_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.mod.tail_jsonl(Path(tmp) / "events.jsonl", 10), [])

    def test_tail_skips_blank_keeps_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text('\n{"ok": true}\nnot-json\n\n{"ok": 2}\n', encoding="utf-8")
            events = self.mod.tail_jsonl(path, limit=10)
            self.assertEqual(len(events), 3)
            self.assertEqual(events[0], {"ok": True})
            self.assertEqual(events[1]["class"], "EVENT_MALFORMED")
            self.assertEqual(events[2], {"ok": 2})


if __name__ == "__main__":
    unittest.main()
