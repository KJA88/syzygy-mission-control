"""Synthetic Fitbit workout normalization. No live health records."""
import importlib.util
import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from workouts.fitbit import (
    CACHE_TTL_S,
    FitbitMcp,
    FitbitUnavailable,
    WorkoutSource,
    dedupe_exercises,
    history_rows,
    normalize_exercise,
    unwrap_tool_result,
)

ROOT = Path(__file__).resolve().parents[1]
FIELDS = {
    "source", "source_key", "name", "exercise_type", "start", "end",
    "duration_minutes", "calories", "steps", "distance_miles",
    "average_heart_rate_bpm", "active_zone_minutes", "heart_rate_zones",
    "average_pace_seconds_per_meter", "average_pace_minutes_per_mile",
    "has_gps", "device", "platform", "recording_method",
}


def _server():
    spec = importlib.util.spec_from_file_location("mc_workout_server", ROOT / "ui" / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sample(**overrides):
    record = {
        "exercise": "Sample Walk",
        "type": "WALKING",
        "start": "2020-01-02T15:00:00-08:00",
        "end": "2020-01-02T15:30:00-08:00",
        "duration_minutes": 30,
        "calories": 100,
        "steps": 3000,
        "distance_miles": 1.5,
        "average_heart_rate_bpm": 110,
        "active_zone_minutes": 12,
        "heart_rate_zones": {
            "light_minutes": 10,
            "moderate_minutes": 8,
            "vigorous_minutes": 2,
            "peak_minutes": 0,
        },
        "average_pace_seconds_per_meter": 0.5,
        "average_pace_minutes_per_mile": 13.41,
        "has_gps": False,
        "device": "Sample Tracker",
        "platform": "FITBIT",
        "recording_method": "AUTOMATIC",
        "additional_metrics": {"unparsed": 1},
        "max_heart_rate_bpm": 180,
    }
    record.update(overrides)
    return record


def fixed_now():
    return datetime(2020, 1, 8, 12, 0, tzinfo=timezone(timedelta(hours=-8)))


class NormalizeTests(unittest.TestCase):
    def test_keeps_parser_fields_and_drops_unknown_ones(self):
        workout = normalize_exercise(sample(active_zone_minutes={"sumInFatBurnHeartZone": 4}))
        self.assertEqual(set(workout), FIELDS)
        self.assertEqual(workout["source"], "fitbit")
        self.assertEqual(workout["name"], "Sample Walk")
        self.assertEqual(workout["exercise_type"], "WALKING")
        self.assertEqual(workout["average_pace_minutes_per_mile"], 13.41)
        self.assertIsNone(workout["active_zone_minutes"])
        self.assertNotIn("max_heart_rate_bpm", workout)
        self.assertNotIn("additional_metrics", workout)

    def test_repeated_reads_collapse_the_same_source_key(self):
        other = sample(start="2020-01-03T15:00:00-08:00", end="2020-01-03T15:20:00-08:00", steps=None, calories=40)
        once = dedupe_exercises([sample(), sample(), other])
        twice = dedupe_exercises([sample(), sample(), other])
        self.assertEqual(len(once), 2)
        self.assertEqual(once, twice)
        self.assertEqual(once[0]["start"], "2020-01-03T15:00:00-08:00")
        self.assertIsNone(once[0]["steps"])

    def test_seven_day_totals_skip_missing_numbers_and_outside_days(self):
        calls = []

        def call_tool(name, arguments):
            calls.append((name, arguments))
            if name == "get_fitbit_exercise_history":
                return [
                    sample(),
                    sample(),
                    sample(start="2020-01-01T15:00:00-08:00", end="2020-01-01T15:10:00-08:00", steps=9, calories=9),
                    sample(start="2020-01-03T15:00:00-08:00", end="2020-01-03T15:20:00-08:00", steps=None, calories=40, distance_miles=0.5, duration_minutes=20, active_zone_minutes=3),
                ]
            if name == "get_fitbit_active_zone_minutes_history":
                return [{
                    "date": "2020-01-02",
                    "active_zone_minutes": 20,
                    "fat_burn_zone_minutes": 10,
                    "cardio_zone_minutes": 8,
                    "peak_zone_minutes": 2,
                }]
            if name == "get_fitbit_time_in_heart_rate_zone_history":
                return [{"date": "2020-01-02", "zones": [{"zone": "FAT_BURN", "duration_seconds": 120}]}]
            raise AssertionError(name)

        summary = WorkoutSource(call_tool, now=fixed_now).summary()
        self.assertEqual(summary["start_date"], "2020-01-02")
        self.assertEqual(summary["end_date"], "2020-01-08")
        self.assertEqual(summary["workout_count"], 2)
        self.assertEqual(summary["totals"]["steps"], 3000)
        self.assertEqual(summary["totals"]["calories"], 140)
        self.assertEqual(summary["totals"]["duration_minutes"], 50)
        self.assertEqual(summary["totals"]["distance_miles"], 2)
        self.assertEqual(summary["totals"]["active_zone_minutes"], 15)
        self.assertEqual(summary["totals"]["heart_rate_zones"]["light_minutes"], 20)
        self.assertEqual(summary["daily_active_zone_minutes"]["totals"]["fat_burn_zone_minutes"], 10)
        self.assertEqual(summary["daily_time_in_heart_rate_zone"]["duration_seconds"]["FAT_BURN"], 120)
        self.assertEqual(calls[0][0], "get_fitbit_exercise_history")
        self.assertNotIn("exercise_type", calls[0][1])

    def test_daily_history_wrapper_reads_records_not_the_envelope(self):
        envelope = {
            "start_date": "2020-01-02",
            "end_date": "2020-01-08",
            "records": [
                {
                    "date": "2020-01-02",
                    "active_zone_minutes": 20,
                    "fat_burn_zone_minutes": 10,
                    "cardio_zone_minutes": 8,
                    "peak_zone_minutes": 2,
                },
                {
                    "date": "2020-01-03",
                    "active_zone_minutes": 5,
                    "fat_burn_zone_minutes": 4,
                    "cardio_zone_minutes": 1,
                    "peak_zone_minutes": 0,
                },
            ],
        }
        zones = {
            "start_date": "2020-01-02",
            "end_date": "2020-01-08",
            "records": [
                {"date": "2020-01-02", "zones": [{"zone": "FAT_BURN", "duration_seconds": 120}]},
                {"date": "2020-01-03", "zones": [{"zone": "FAT_BURN", "duration_seconds": 30}]},
            ],
        }
        self.assertEqual(len(history_rows(envelope)), 2)
        self.assertNotIn("start_date", history_rows(envelope)[0])

        def call_tool(name, arguments):
            if name == "get_fitbit_exercise_history":
                return [sample()]
            if name == "get_fitbit_active_zone_minutes_history":
                return envelope
            if name == "get_fitbit_time_in_heart_rate_zone_history":
                return zones
            raise AssertionError(name)

        summary = WorkoutSource(call_tool, now=fixed_now).summary()
        totals = summary["daily_active_zone_minutes"]
        self.assertEqual(len(totals["days"]), 2)
        self.assertEqual(totals["totals"]["active_zone_minutes"], 25)
        self.assertEqual(totals["totals"]["fat_burn_zone_minutes"], 14)
        self.assertEqual(summary["daily_time_in_heart_rate_zone"]["duration_seconds"]["FAT_BURN"], 150)
        self.assertEqual([day["date"] for day in totals["days"]], ["2020-01-02", "2020-01-03"])

    def test_summary_cache_blocks_repeat_history_calls_until_ttl(self):
        clock = {"now": 0.0}
        calls = []

        def call_tool(name, arguments):
            calls.append(name)
            if name == "get_fitbit_exercise_history":
                return [sample()]
            if name == "get_fitbit_active_zone_minutes_history":
                return {"start_date": "2020-01-02", "end_date": "2020-01-08", "records": []}
            if name == "get_fitbit_time_in_heart_rate_zone_history":
                return {"start_date": "2020-01-02", "end_date": "2020-01-08", "records": []}
            raise AssertionError(name)

        source = WorkoutSource(call_tool, now=fixed_now, clock=lambda: clock["now"])
        first = source.summary()
        second = source.summary()
        self.assertEqual(first, second)
        self.assertEqual(calls.count("get_fitbit_exercise_history"), 1)
        self.assertEqual(calls.count("get_fitbit_active_zone_minutes_history"), 1)
        clock["now"] = CACHE_TTL_S
        source.summary()
        self.assertEqual(calls.count("get_fitbit_exercise_history"), 2)
        self.assertGreaterEqual(CACHE_TTL_S, 5 * 60)
        self.assertLessEqual(CACHE_TTL_S, 15 * 60)

    def test_daily_rollup_failure_keeps_the_workout_list(self):
        def call_tool(name, arguments):
            if name == "get_fitbit_exercise_history":
                return [sample()]
            raise RuntimeError("rollup down")

        summary = WorkoutSource(call_tool, now=fixed_now).summary()
        self.assertTrue(summary["available"])
        self.assertEqual(summary["workout_count"], 1)
        self.assertFalse(summary["daily_active_zone_minutes"]["available"])
        self.assertFalse(summary["daily_time_in_heart_rate_zone"]["available"])
        self.assertNotIn("rollup down", json.dumps(summary))

    def test_source_failure_has_no_body_text(self):
        def call_tool(name, arguments):
            raise RuntimeError("token secret-value")

        recent = WorkoutSource(call_tool, now=fixed_now).recent(5)
        self.assertFalse(recent["available"])
        self.assertEqual(recent["reason"], "FITBIT_UNAVAILABLE")
        self.assertNotIn("secret-value", json.dumps(recent))


class McpClientTests(unittest.TestCase):
    def test_public_url_and_unknown_tool_do_not_call_transport(self):
        def transport(*_args):
            raise AssertionError("transport called")

        with self.assertRaises(FitbitUnavailable):
            FitbitMcp("https://fitbit.syzygylab.net/mcp", transport=transport)
        client = FitbitMcp(transport=transport)
        with self.assertRaises(FitbitUnavailable):
            client.call_tool("get_fitbit_data_quality", {})

    def test_tool_call_uses_loopback_and_unwraps_text_json(self):
        seen = []

        def transport(url, payload, headers, timeout):
            seen.append((url, payload, dict(headers)))
            self.assertNotIn("Authorization", headers)
            if payload["method"] == "initialize":
                body = {"jsonrpc": "2.0", "id": 1, "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "serverInfo": {"name": "Fitbit Health MCP"},
                }}
                return 200, {"Mcp-Session-Id": "session-1"}, json.dumps(body).encode()
            if payload["method"] == "notifications/initialized":
                return 202, {}, b""
            body = {"jsonrpc": "2.0", "id": 2, "result": {"content": [{"type": "text", "text": "[]"}]}}
            return 200, {}, json.dumps(body).encode()

        result = FitbitMcp(transport=transport).call_tool("get_fitbit_exercises", {"limit": 2})
        self.assertEqual(result, [])
        self.assertEqual(seen[0][0], "http://127.0.0.1:8010/mcp")
        self.assertEqual(seen[2][1]["params"]["name"], "get_fitbit_exercises")
        self.assertEqual(seen[2][2]["Mcp-Session-Id"], "session-1")

    def test_structured_content_unwrap(self):
        message = {"jsonrpc": "2.0", "id": 2, "result": {"structuredContent": {"result": [{"exercise": "Sample Walk"}]}}}
        self.assertEqual(unwrap_tool_result(message), [{"exercise": "Sample Walk"}])


class RouteTests(unittest.TestCase):
    def test_routes_are_read_only_and_uncached(self):
        class Fake:
            def recent(self, limit):
                return {"available": True, "source": "fitbit", "workouts": dedupe_exercises([sample()])[:limit]}

            def summary(self):
                return {"available": True, "source": "fitbit", "workout_count": 1, "workouts": [], "totals": {}}

        server = _server()
        handler = server.make_handler(ROOT / "state", ROOT / "ui", 120, 20, workouts=Fake())
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % httpd.server_port
        try:
            with urllib.request.urlopen(base + "/api/workouts/recent?limit=8", timeout=2) as response:
                self.assertEqual(response.headers.get("Cache-Control"), "no-store")
                payload = json.loads(response.read().decode())
            self.assertEqual(payload["workouts"][0]["name"], "Sample Walk")
            with urllib.request.urlopen(base + "/api/workouts/summary?days=7", timeout=2) as response:
                self.assertEqual(json.loads(response.read().decode())["workout_count"], 1)
            request = urllib.request.Request(base + "/api/workouts/recent", data=b"{}", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(caught.exception.code, 405)
            caught.exception.close()
            with self.assertRaises(urllib.error.HTTPError) as malformed:
                urllib.request.urlopen(base + "/api/workouts/summary?days=3", timeout=2)
            self.assertEqual(malformed.exception.code, 400)
            malformed.exception.close()
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
