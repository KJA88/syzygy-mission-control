from copy import deepcopy
import unittest

from guardian.state_engine import assertion, build_state


class StateEngineTests(unittest.TestCase):
    def snapshot(self):
        fact = {"status": "green", "required": True,
                "observed_at": "2026-09-24T20:00:00Z", "observation_age_s": 2,
                "trace_id": "trace-1", "class": None}
        camera = dict(fact)
        return {
            "schema_version": 1,
            "generated_at": "2026-09-24T20:00:02Z",
            "trace_id": "trace-1",
            "guardian": dict(fact),
            "system": {"status": "green", "reason": None},
            "nodes": [dict(fact, id="pi", metrics={"ram_free_gb": 5.5,
                                                     "gpu_percent": None})],
            "services": [dict(fact, id="tv-mcp", host="pi", cameras={}),
                         dict(fact, id="vision", host="jetson",
                              cameras={"indoor": camera})],
        }

    def test_builds_read_only_versioned_state_without_mutating_snapshot(self):
        source = self.snapshot()
        original = deepcopy(source)
        state = build_state(source)
        self.assertEqual(source, original)
        self.assertEqual(state["schema_version"], 1)
        self.assertTrue(state["read_only"])
        self.assertEqual(state["trace_id"], "trace-1")

    def test_preserves_provenance_and_explicit_unknowns(self):
        state = build_state(self.snapshot())
        entities = {item["id"]: item for item in state["entities"]}
        tv = entities["service/tv-mcp"]["attributes"]
        self.assertEqual(tv["health"]["value"], "green")
        self.assertEqual(tv["health"]["knowledge"], "derived")
        self.assertEqual(tv["verified_input"]["knowledge"], "unknown")
        self.assertIsNone(tv["verified_input"]["value"])
        self.assertIsNone(tv["verified_input"]["confidence"])
        self.assertEqual(tv["verified_input"]["reason"], "ADAPTER_NOT_IMPLEMENTED")
        self.assertEqual(tv["requested_input"]["knowledge"], "unknown")
        self.assertNotIn(
            "verified",
            {attribute["knowledge"] for entity in state["entities"]
             for attribute in entity["attributes"].values()},
        )

    def test_node_metrics_are_observations_and_camera_online_is_derived(self):
        state = build_state(self.snapshot())
        entities = {item["id"]: item for item in state["entities"]}
        metric = entities["node/pi"]["attributes"]["metric/ram_free_gb"]
        self.assertEqual(metric["knowledge"], "observed")
        self.assertEqual(metric["value"], 5.5)
        unavailable = entities["node/pi"]["attributes"]["metric/gpu_percent"]
        self.assertEqual(unavailable["knowledge"], "unknown")
        self.assertEqual(unavailable["reason"], "METRIC_UNAVAILABLE")
        online = entities["camera/indoor"]["attributes"]["online"]
        self.assertEqual(online["knowledge"], "derived")
        self.assertTrue(online["value"])

    def test_configuration_is_not_mislabeled_as_observation(self):
        state = build_state(self.snapshot())
        tv = next(item for item in state["entities"] if item["id"] == "service/tv-mcp")
        self.assertEqual(tv["attributes"]["host"]["knowledge"], "configured")
        self.assertEqual(tv["attributes"]["host"]["source"], "guardian/config")
        self.assertIsNone(tv["attributes"]["host"]["observed_at"])

    def test_unknown_or_stale_health_never_becomes_green(self):
        snap = self.snapshot()
        snap["nodes"][0].update(
            status="unknown", observed_at="2026-09-24T19:58:00Z",
            trace_id="node-stale-trace", **{"class": "EVIDENCE_STALE"})
        state = build_state(snap)
        pi = next(item for item in state["entities"] if item["id"] == "node/pi")
        health = pi["attributes"]["health"]
        self.assertEqual(health["knowledge"], "unknown")
        self.assertIsNone(health["value"])
        self.assertEqual(health["freshness"], "stale")
        self.assertEqual(health["observed_at"], "2026-09-24T19:58:00Z")
        self.assertEqual(health["trace_id"], "node-stale-trace")

    def test_late_evidence_and_system_staleness_are_not_current(self):
        snap = self.snapshot()
        snap["nodes"][0].update(status="yellow", **{"class": "AGENT_LATE"})
        snap["system"].update(status="unknown", reason="GUARDIAN_HEARTBEAT_STALE")
        state = build_state(snap)
        entities = {item["id"]: item for item in state["entities"]}
        self.assertEqual(
            entities["node/pi"]["attributes"]["health"]["freshness"], "stale")
        self.assertEqual(
            entities["node/pi"]["attributes"]["metric/ram_free_gb"]["freshness"],
            "stale")
        system_health = entities["system/syzygy"]["attributes"]["health"]
        self.assertEqual(system_health["freshness"], "stale")
        self.assertEqual(system_health["reason"], "GUARDIAN_HEARTBEAT_STALE")

    def test_missing_host_and_malformed_metrics_remain_unknown(self):
        snap = self.snapshot()
        snap["nodes"][0]["metrics"] = ["not", "a", "mapping"]
        snap["services"][0].pop("host")
        state = build_state(snap)
        entities = {item["id"]: item for item in state["entities"]}
        self.assertNotIn(
            "metric/ram_free_gb", entities["node/pi"]["attributes"])
        host = entities["service/tv-mcp"]["attributes"]["host"]
        self.assertEqual(host["knowledge"], "unknown")
        self.assertEqual(host["reason"], "CONFIG_MISSING")

    def test_assertion_rejects_invalid_semantics(self):
        with self.assertRaises(ValueError):
            assertion("x", "guessed", "fresh", "test", None, "trace")
        with self.assertRaises(ValueError):
            assertion("x", "observed", "forever", "test", None, "trace")
        with self.assertRaises(ValueError):
            assertion("x", "observed", "fresh", "test", None, "trace", confidence=1.5)
        with self.assertRaises(ValueError):
            assertion(None, "observed", "fresh", "test", None, "trace")

    def test_unknown_assertions_always_clear_value_and_confidence(self):
        result = assertion(
            "untrusted", "unknown", "stale", "test", "2026-09-24T20:00:00Z",
            "trace", confidence=0.9)
        self.assertIsNone(result["value"])
        self.assertIsNone(result["confidence"])


if __name__ == "__main__":
    unittest.main()
