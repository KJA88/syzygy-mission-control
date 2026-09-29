"""Synthetic Fitbit health normalization. No live health records."""
import importlib.util
import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from health.fitbit import CACHE_TTL_S, HEALTH_TOOLS, HealthSource, iso_date
from workouts.fitbit import ALLOWED_TOOLS, FitbitMcp, FitbitUnavailable

ROOT = Path(__file__).resolve().parents[1]


def _server():
    spec = importlib.util.spec_from_file_location("mc_health_server", ROOT / "ui" / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixed_now():
    return datetime(2020, 1, 8, 12, 0, tzinfo=timezone(timedelta(hours=-8)))


def _date(year=2020, month=1, day=8):
    return {"year": year, "month": month, "day": day}


class Catalog:
    def __init__(self, payloads=None, fail=()):
        self.payloads = payloads or {}
        self.fail = set(fail)
        self.calls = []

    def __call__(self, name, arguments):
        self.calls.append((name, arguments))
        if name in self.fail:
            raise RuntimeError("secret-value")
        if name not in self.payloads:
            raise RuntimeError("missing-tool")
        return self.payloads[name]


def sample_catalog():
    return {
        "get_fitbit_sleep": [{
            "local_wake_date": "2020-01-07",
            "sleep_role": "main_sleep",
            "main_sleep": True,
            "minutes_asleep": 400,
            "minutes_awake": 20,
            "sleep_stage_totals": [{"type": "deep", "minutes": 60, "count": 2}],
            "sleep_stage_timeline": [{"start": "secret-interval"}],
            "device": "Sample Tracker",
            "platform": "FITBIT",
            "quality_status": "valid",
        }],
        "get_fitbit_resting_heart_rate": [{"date": _date(), "resting_heart_rate_bpm": 60, "platform": "FITBIT"}],
        "get_fitbit_hrv": [{
            "date": _date(),
            "average_hrv_ms": 42,
            "deep_sleep_rmssd_ms": 30,
            "quality_status": "valid",
            "platform": "FITBIT",
        }],
        "get_fitbit_oxygen_saturation": [{"date": _date(day=7), "average_spo2_percent": 97}],
        "get_fitbit_respiratory_rate": [{"date": _date(), "breaths_per_minute": 14}],
        "get_fitbit_sleep_temperature": [{"date": _date(), "difference_from_baseline_celsius": -0.2}],
        "get_fitbit_weight": [],
        "get_fitbit_heart_rate": [{"local_date": _date(), "heart_rate_bpm": 70}],
        "get_fitbit_heart_rate_zones": [{"date": _date(), "zones": [{"type": "CARDIO", "min_bpm": 100, "max_bpm": 140}]}],
        "get_fitbit_steps": {"date": "2020-01-08", "steps": 1000, "raw_points": [{"secret": True}]},
        "get_fitbit_distance": {"date": "2020-01-08", "distance_miles": 1.25},
        "get_fitbit_active_zone_minutes": {
            "date": "2020-01-08",
            "active_zone_minutes": 15,
            "fat_burn_zone_minutes": 10,
            "cardio_zone_minutes": 5,
            "peak_zone_minutes": 0,
        },
        "get_fitbit_total_calories": {"date": "2020-01-08", "total_calories_kcal": 1800},
        "get_fitbit_active_energy_burned": {"date": "2020-01-08", "active_energy_kcal": 200},
        "get_fitbit_floors": {"date": "2020-01-08", "floors": 4},
        "get_fitbit_active_minutes": {
            "date": "2020-01-08",
            "active_minutes_total": 30,
            "by_activity_level": {"light": 20, "moderate": 10},
        },
        "get_fitbit_time_in_heart_rate_zone": {
            "date": "2020-01-08",
            "zones": [{"zone": "FAT_BURN", "duration_seconds": 120}],
        },
    }


class NormalizeTests(unittest.TestCase):
    def test_iso_date_uses_parser_parts_only(self):
        self.assertEqual(iso_date(_date()), "2020-01-08")
        self.assertEqual(iso_date("2020-01-08T12:00:00-08:00"), "2020-01-08")
        self.assertIsNone(iso_date({"year": 2020, "month": 1}))

    def test_today_keeps_source_fields_and_drops_raw_blobs(self):
        catalog = Catalog(sample_catalog())
        payload = HealthSource(catalog, now=fixed_now).today()
        encoded = json.dumps(payload)
        self.assertNotIn("raw_points", encoded)
        self.assertNotIn("secret-interval", encoded)
        self.assertNotIn("secret-value", encoded)
        self.assertEqual(payload["source"], "fitbit")
        metrics = {item["name"]: item for item in payload["metrics"]}
        self.assertEqual(metrics["resting_heart_rate_bpm"]["value"], 60)
        self.assertEqual(metrics["resting_heart_rate_bpm"]["freshness"], "present")
        self.assertEqual(metrics["average_hrv_ms"]["source"], "fitbit")
        self.assertEqual(metrics["average_hrv_ms"]["value"], 42)
        self.assertEqual(metrics["minutes_asleep"]["freshness"], "present")
        self.assertEqual(metrics["minutes_asleep"]["date"], "2020-01-07")
        self.assertEqual(metrics["average_spo2_percent"]["freshness"], "not_today")
        self.assertEqual(metrics["steps"]["value"], 1000)
        self.assertEqual(metrics["active_minutes_total"]["by_activity_level"]["light"], 20)
        self.assertEqual(payload["sleep"]["sleep_stage_totals"][0]["type"], "deep")
        self.assertEqual(payload["recovery"]["series"], "fitbit_nightly_hrv")
        self.assertIn("weight_pounds", [item["name"] for item in payload["watchlist"]])
        self.assertIn("average_spo2_percent", [item["name"] for item in payload["watchlist"]])
        self.assertTrue(payload["available"])

    def test_history_wrapper_and_bare_list_both_sum(self):
        payloads = {
            "get_fitbit_steps_history": {
                "start_date": "2020-01-02",
                "end_date": "2020-01-08",
                "records": [
                    {"date": "2020-01-02", "steps": 100, "raw_points": [1]},
                    {"date": "2020-01-03", "steps": 50},
                    {"date": "2020-01-01", "steps": 9},
                ],
            },
            "get_fitbit_resting_heart_rate_history": [
                {"date": _date(day=2), "resting_heart_rate_bpm": 61},
                {"date": _date(day=8), "resting_heart_rate_bpm": 59},
            ],
            "get_fitbit_time_in_heart_rate_zone_history": {
                "start_date": "2020-01-02",
                "end_date": "2020-01-08",
                "records": [
                    {"date": "2020-01-02", "zones": [{"zone": "FAT_BURN", "duration_seconds": 60}]},
                    {"date": "2020-01-03", "zones": [{"zone": "FAT_BURN", "duration_seconds": 30}]},
                ],
            },
        }
        catalog = Catalog(payloads, fail=["get_fitbit_hrv_history"])
        summary = HealthSource(catalog, now=fixed_now).summary()
        self.assertEqual(summary["series"]["steps"]["total"], 150)
        self.assertEqual(summary["series"]["steps"]["sample_count"], 2)
        self.assertNotIn("raw_points", json.dumps(summary))
        self.assertEqual(summary["series"]["resting_heart_rate_bpm"]["latest"]["value"], 59)
        self.assertIsNone(summary["series"]["resting_heart_rate_bpm"]["total"])
        self.assertFalse(summary["series"]["average_hrv_ms"]["available"])
        self.assertEqual(summary["time_in_heart_rate_zone_seconds"]["duration_seconds"]["FAT_BURN"], 90)
        self.assertTrue(summary["available"])
        self.assertNotIn("secret-value", json.dumps(summary))

    def test_source_failure_stays_on_the_health_payload(self):
        catalog = Catalog(fail=HEALTH_TOOLS)
        payload = HealthSource(catalog, now=fixed_now).today()
        self.assertFalse(payload["available"])
        self.assertEqual(payload["reason"], "FITBIT_UNAVAILABLE")
        self.assertNotIn("secret-value", json.dumps(payload))

    def test_cache_holds_health_calls_for_ten_minutes(self):
        clock = {"now": 0.0}
        catalog = Catalog(sample_catalog())
        source = HealthSource(catalog, now=fixed_now, clock=lambda: clock["now"])
        self.assertEqual(source.today(), source.today())
        self.assertEqual(catalog.calls.count(("get_fitbit_hrv", {"limit": 5})), 1)
        clock["now"] = CACHE_TTL_S
        source.today()
        self.assertEqual(catalog.calls.count(("get_fitbit_hrv", {"limit": 5})), 2)
        self.assertGreaterEqual(CACHE_TTL_S, 5 * 60)
        self.assertLessEqual(CACHE_TTL_S, 15 * 60)

    def test_workout_client_does_not_gain_health_tools(self):
        self.assertNotIn("get_fitbit_hrv", ALLOWED_TOOLS)

        def transport(*_args):
            raise AssertionError("transport called")

        with self.assertRaises(FitbitUnavailable):
            FitbitMcp(transport=transport).call_tool("get_fitbit_hrv", {"limit": 1})


class RouteTests(unittest.TestCase):
    def test_health_routes_are_read_only_and_leave_mission_health(self):
        class Fake:
            def today(self):
                return {"available": True, "source": "fitbit", "metrics": [{"name": "steps", "value": 1000}]}

            def summary(self):
                return {"available": True, "source": "fitbit", "days": 7, "series": {}}

            def trends(self):
                return {"available": True, "source": "fitbit", "days": 30, "series": {}}

        server = _server()
        handler = server.make_handler(ROOT / "state", ROOT / "ui", 120, 20, health=Fake())
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % httpd.server_port
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2) as response:
                mission = json.loads(response.read().decode())
            self.assertTrue(mission["ok"])
            self.assertIn("mission_engine", mission)
            with urllib.request.urlopen(base + "/api/health/today", timeout=2) as response:
                self.assertEqual(response.headers.get("Cache-Control"), "no-store")
                today = json.loads(response.read().decode())
            self.assertEqual(today["metrics"][0]["value"], 1000)
            with urllib.request.urlopen(base + "/api/health/summary?days=7", timeout=2) as response:
                self.assertEqual(json.loads(response.read().decode())["days"], 7)
            with urllib.request.urlopen(base + "/api/health/trends?days=30", timeout=2) as response:
                self.assertEqual(json.loads(response.read().decode())["days"], 30)
            with self.assertRaises(urllib.error.HTTPError) as malformed:
                urllib.request.urlopen(base + "/api/health/summary?days=30", timeout=2)
            self.assertEqual(malformed.exception.code, 400)
            malformed.exception.close()
            request = urllib.request.Request(base + "/api/health/today", data=b"{}", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(denied.exception.code, 405)
            denied.exception.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
