"""Read-only health view tests. Synthetic rows only."""

import json
import threading
import unittest
import urllib.error
import urllib.request
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path

import ui.server as server
from health.view import build_health, read_token, source_label
from health_workbook.feeders import WorkbookClient

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 3)


def _daily(day, **fields):
    row = {"date": day, "updated_by": fields.pop("updated_by", "fitbit-sync")}
    row.update(fields)
    return row


def _bp(stamp, systolic, diastolic, source="WITHINGS"):
    return {
        "timestamp": stamp,
        "metric": "blood_pressure",
        "value": systolic,
        "value2": diastolic,
        "unit": "mmHg",
        "source": source,
        "device": "BPM_CONNECT",
        "updated_by": "withings-sync",
    }


class HealthBuildTests(unittest.TestCase):
    def test_daily_cards_measurements_and_separate_bp_readings(self):
        payload = build_health([
            _daily("2026-10-02", weight_lb=180, body_fat_pct=20, rhr_bpm=58, hrv_ms=42,
                   sleep_asleep_min=400, steps=8000, active_kcal_fitbit=500, fitbit_total_burn=2400),
            _daily("2026-10-03", weight_lb=181, updated_by="withings-sync", steps=1000),
        ], [
            _bp("2026-10-03T08:00:00-07:00", 128, 82),
            _bp("2026-10-03T18:00:00-07:00", 121, 79),
            {
                "timestamp": "2026-10-03T08:05:00-07:00",
                "metric": "weight",
                "value": 181,
                "unit": "lb",
                "source": "WITHINGS",
                "device": "BODY_SMART",
            },
        ], TODAY)
        cards = {card["id"]: card for card in payload["cards"]}
        self.assertEqual(cards["weight"]["value"], 181)
        self.assertEqual(cards["weight"]["source"], "WITHINGS")
        self.assertEqual(cards["body_fat"]["date"], "2026-10-02")
        self.assertEqual(cards["body_fat"]["source"], "FITBIT")
        self.assertEqual(cards["resting_hr"]["value"], 58)
        self.assertEqual(cards["hrv"]["value"], 42)
        self.assertEqual(cards["sleep"]["value"], 400)
        self.assertEqual(cards["steps"]["value"], 1000)
        self.assertEqual(cards["active_calories"]["value"], 500)
        self.assertEqual(cards["total_burn"]["value"], 2400)
        self.assertEqual(cards["blood_pressure"]["value"], 121)
        self.assertEqual(cards["blood_pressure"]["value2"], 79)
        self.assertFalse(cards["glucose"]["present"])
        recent_bp = [row for row in payload["recent"] if row["metric"] == "blood_pressure"]
        self.assertEqual([(row["value"], row["value2"]) for row in recent_bp], [(121, 79), (128, 82)])
        self.assertEqual(len(payload["trends"]["blood_pressure"]["days_7"]), 2)
        self.assertEqual(payload["trends"]["weight"]["days_7"][-1]["value"], 181)
        self.assertEqual(len(payload["trends"]["glucose"]["days_30"]), 0)
        self.assertNotIn("secret", json.dumps(payload))

    def test_active_calorie_fallback_and_old_points_stay_in_thirty_days(self):
        payload = build_health([
            _daily("2026-09-04", weight_lb=170),
            _daily("2026-10-01", fitbit_active_burn=320, updated_by="fitbit-sync"),
        ], [], TODAY)
        cards = {card["id"]: card for card in payload["cards"]}
        self.assertEqual(cards["active_calories"]["value"], 320)
        self.assertEqual(cards["active_calories"]["source"], "FITBIT")
        self.assertEqual(payload["trends"]["weight"]["days_7"], [])
        self.assertEqual(payload["trends"]["weight"]["days_30"][0]["value"], 170)

    def test_source_labels_and_token_file_does_not_return_the_secret(self):
        self.assertEqual(source_label("POLAR_H10", "other"), "POLAR_H10")
        self.assertEqual(source_label(None, "manual"), "MANUAL")
        self.assertIsNone(source_label(None, None))
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.env"
            path.write_text(
                "HEALTH_WORKBOOK_READ_TOKEN=read-secret\nHEALTH_WORKBOOK_MAINTAIN_TOKEN=maintain-secret\n",
                encoding="utf-8",
            )
            token = read_token({}, str(path))
            self.assertEqual(token, "read-secret")
            self.assertNotIn("maintain-secret", token)

    def test_non_loopback_workbook_url_is_rejected(self):
        with self.assertRaises(Exception) as caught:
            WorkbookClient("https://example.com", "read-secret")
        self.assertNotIn("read-secret", str(caught.exception))


class HealthRouteTests(unittest.TestCase):
    def test_summary_is_read_only(self):
        class Fake:
            def summary(self):
                return build_health([_daily("2026-10-03", weight_lb=181, updated_by="withings-sync")], [], TODAY)

        handler = server.make_handler(ROOT / "state", ROOT / "ui", 120, 20, health=Fake())
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % httpd.server_port
        try:
            with urllib.request.urlopen(base + "/api/health/summary", timeout=2) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("no-store", response.headers.get("Cache-Control", ""))
                payload = json.loads(response.read().decode())
            self.assertTrue(payload["available"])
            self.assertEqual(payload["cards"][0]["id"], "weight")
            request = urllib.request.Request(base + "/api/health/summary", data=b"{}", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(caught.exception.code, 405)
            caught.exception.close()
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
