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
        original = repr(source)
        state = build_state(source)
        self.assertEqual(repr(source), original)
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
        self.assertEqual(tv["verified_input"]["reason"], "ADAPTER_NOT_IMPLEMENTED")

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

    def test_unknown_or_stale_health_never_becomes_green(self):
        snap = self.snapshot()
        snap["nodes"][0].update(status="unknown", **{"class": "EVIDENCE_STALE"})
        state = build_state(snap)
        pi = next(item for item in state["entities"] if item["id"] == "node/pi")
        self.assertEqual(pi["attributes"]["health"]["knowledge"], "unknown")
        self.assertIsNone(pi["attributes"]["health"]["value"])

    def test_assertion_rejects_invalid_semantics(self):
        with self.assertRaises(ValueError):
            assertion("x", "guessed", "fresh", "test", None, "trace")
        with self.assertRaises(ValueError):
            assertion("x", "observed", "forever", "test", None, "trace")
        with self.assertRaises(ValueError):
            assertion("x", "observed", "fresh", "test", None, "trace", confidence=1.5)


if __name__ == "__main__":
    unittest.main()
