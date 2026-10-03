"""Withings feeder tests. Synthetic readings only."""

import gc
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from health_workbook.feeders import WorkbookClient, zone
from health_workbook.mutate import add_measurements_sheet
from health_workbook.service import Config, build_logger, serve
from health_workbook.withings_feeder import (
    WithingsClient,
    WithingsError,
    kg_to_lb,
    map_withings,
    sync_withings,
)
from health_workbook.workbook import load_workbook
from tests.test_health_workbook import TOKEN, fixture_sheets, write_workbook

MAINTAIN = "phase2a-maintain-token"
RECORDED = "2026-01-05T18:00:00Z"
DAY = "2026-01-05"


def _epoch(day: str, hour: int = 8) -> int:
    year, month, date = (int(part) for part in day.split("-"))
    return int(datetime(year, month, date, hour, tzinfo=zone()).timestamp())


def _devices():
    return [
        {"deviceid": "scale-1", "model": "Body Smart", "model_id": 13},
        {"deviceid": "bp-1", "model": "BPM Connect", "model_id": 45},
        {"deviceid": "other-1", "model": "Body Cardio", "model_id": 6},
    ]


def _scale(grpid, hour=8, fat=True):
    measures = [{"type": 1, "value": 80000, "unit": -3}]
    if fat:
        measures.extend([
            {"type": 6, "value": 205, "unit": -1},
            {"type": 8, "value": 16000, "unit": -3},
            {"type": 5, "value": 64000, "unit": -3},
            {"type": 76, "value": 60000, "unit": -3},
            {"type": 88, "value": 3000, "unit": -3},
            {"type": 77, "value": 48000, "unit": -3},
            {"type": 170, "value": 8, "unit": 0},
            {"type": 226, "value": 1700, "unit": 0},
        ])
    return {"grpid": grpid, "deviceid": "scale-1", "date": _epoch(DAY, hour), "measures": measures}


def _bpm(grpid, hour, systolic, diastolic, pulse):
    return {
        "grpid": grpid,
        "deviceid": "bp-1",
        "date": _epoch(DAY, hour),
        "measures": [
            {"type": 10, "value": systolic, "unit": 0},
            {"type": 9, "value": diastolic, "unit": 0},
            {"type": 11, "value": pulse, "unit": 0},
        ],
    }


class WithingsMappingTests(unittest.TestCase):
    def test_body_smart_bpm_and_missing_metric(self):
        readings, daily, skipped = map_withings(_devices(), [
            _scale(42),
            _scale(43, hour=9, fat=False),
            _bpm(70, 10, 128, 82, 71),
            _bpm(71, 18, 121, 79, 66),
            {"grpid": 9, "deviceid": "other-1", "date": _epoch(DAY), "measures": [{"type": 1, "value": 1, "unit": 0}]},
        ], [DAY])
        by_id = {row["external_id"]: row for row in readings}
        weight = by_id["withings:42:weight"]
        self.assertEqual(weight["device"], "BODY_SMART")
        self.assertEqual(weight["source"], "WITHINGS")
        self.assertEqual(weight["updated_by"], "withings-sync")
        self.assertEqual(weight["unit"], "lb")
        self.assertEqual(weight["value"], kg_to_lb(80))
        self.assertEqual(by_id["withings:42:body_fat_pct"]["value"], 20.5)
        self.assertEqual(by_id["withings:42:body_fat_pct"]["unit"], "percent")
        for metric in ("fat_mass", "fat_free_mass", "muscle_mass", "bone_mass", "body_water", "visceral_fat", "bmr"):
            self.assertIn("withings:42:" + metric, by_id)
        self.assertNotIn("withings:43:body_fat_pct", by_id)
        self.assertIn("withings:43:weight", by_id)
        pressure = [row for row in readings if row["metric"] == "blood_pressure"]
        self.assertEqual(len(pressure), 2)
        self.assertEqual(pressure[0]["value"], 128)
        self.assertEqual(pressure[0]["value2"], 82)
        self.assertEqual(pressure[0]["unit"], "mmHg")
        self.assertEqual(pressure[0]["device"], "BPM_CONNECT")
        pulse = [row for row in readings if row["metric"] == "heart_rate"]
        self.assertEqual([row["value"] for row in pulse], [71, 66])
        self.assertNotIn("diastolic", {row["metric"] for row in readings})
        self.assertEqual(daily[DAY]["weight_lb"], kg_to_lb(80))
        self.assertEqual(daily[DAY]["body_fat_pct"], 20.5)
        self.assertGreater(skipped, 0)

    def test_token_refresh_persists_without_returning_secrets(self):
        seen = {"meas": 0}

        def transport(_url, data, headers):
            text = data.decode()
            if "grant_type=refresh_token" in text:
                self.assertNotIn("Authorization", headers)
                return 200, {"status": 0, "body": {"access_token": "secret-access", "refresh_token": "secret-refresh"}}
            if "action=getdevice" in text:
                return 200, {"status": 0, "body": {"devices": []}}
            if "action=getmeas" in text:
                seen["meas"] += 1
                if seen["meas"] == 1:
                    return 200, {"status": 401, "body": {}}
                return 200, {"status": 0, "body": {"measuregrps": []}}
            return 500, {}

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "withings.env"
            path.write_text("WITHINGS_ACCESS_TOKEN=old\nWITHINGS_REFRESH_TOKEN=old\n", encoding="utf-8")
            client = WithingsClient("id", "secret-client", "old-access", "old-refresh", transport, str(path))
            devices, groups = client.load_window([DAY])
            stored = path.read_text(encoding="utf-8")
        self.assertEqual(devices, [])
        self.assertEqual(groups, [])
        self.assertEqual(client.auth, "refreshed")
        self.assertIn("WITHINGS_ACCESS_TOKEN=secret-access", stored)
        self.assertNotIn("secret-access", client.auth)
        self.assertNotIn("secret-client", client.auth)


class WithingsServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._cleanup_tmp)
        self.path = Path(self.tmp.name) / "staged.xlsx"
        write_workbook(self.path, fixture_sheets())
        self.path.write_bytes(add_measurements_sheet(self.path.read_bytes()))
        self.server = serve(Config(self.path, TOKEN, "127.0.0.1", 0, MAINTAIN), build_logger())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.workbook = WorkbookClient(f"http://127.0.0.1:{self.server.server_address[1]}", MAINTAIN)

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _cleanup_tmp(self):
        self._stop_server()
        gc.collect()
        for _ in range(20):
            try:
                self.tmp.cleanup()
                return
            except PermissionError:
                time.sleep(0.05)
        self.tmp.cleanup()

    def _load(self, groups):
        def load():
            return _devices(), groups, "ok"
        return load

    def _same(self, actual, expected):
        if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            self.assertEqual(float(actual), float(expected))
        else:
            self.assertEqual(actual, expected)

    def test_daily_update_replay_and_conflict(self):
        groups = [_scale(42), _bpm(70, 10, 118, 76, 60)]
        first = sync_withings(self._load(groups), self.workbook, [DAY], RECORDED)
        self.assertEqual(first["measurements_appended"], 11)
        self.assertEqual(first["daily_appended"], 1)
        self.assertEqual(first["fields_written"], ["body_fat_pct", "weight_lb"])
        self.assertEqual(first["errors"], 0)
        row = self.workbook.rows("daily", date=DAY)[0]
        self._same(row["weight_lb"], kg_to_lb(80))
        self._same(row["body_fat_pct"], 20.5)
        self.assertIsNone(row["steps"])
        digest = sha256(self.path.read_bytes()).hexdigest()
        again = sync_withings(self._load(groups), self.workbook, [DAY], RECORDED)
        self.assertEqual(again["measurements_exists"], 11)
        self.assertEqual(again["measurements_appended"], 0)
        self.assertEqual(again["daily_exists"], 1)
        self.assertEqual(sha256(self.path.read_bytes()).hexdigest(), digest)
        manual = self.workbook.rows("daily", date="2026-01-02")[0]
        self.workbook.patch("daily", manual["row_id"], {"weight_lb": 200}, "manual weight", "SCALE", "fixture", RECORDED)
        self.workbook.post("measurements", {
            "timestamp": datetime.fromtimestamp(_epoch("2026-01-02"), tz=zone()).isoformat(timespec="seconds"),
            "metric": "weight",
            "value": 150,
            "unit": "lb",
            "source": "WITHINGS",
            "device": "BODY_SMART",
            "external_id": "withings:99:weight",
            "updated_by": "coach",
            "recorded_at": RECORDED,
        })
        conflict_group = _scale(99)
        conflict_group["date"] = _epoch("2026-01-02")
        conflicted = sync_withings(self._load([conflict_group]), self.workbook, ["2026-01-02"], RECORDED)
        self.assertGreater(conflicted["measurements_conflicts"], 0)
        self.assertIn("2026-01-02:weight_lb", conflicted["conflicts"])
        kept = self.workbook.rows("daily", date="2026-01-02")[0]
        self._same(kept["weight_lb"], 200)
        self._same(kept["steps"], 10)
        stored = [row for row in self.workbook.rows("measurements", date="2026-01-02") if row.get("external_id") == "withings:99:weight"]
        self.assertEqual(len(stored), 1)
        self._same(stored[0]["value"], 150)

    def test_api_failure_does_not_change_workbook(self):
        before = sha256(self.path.read_bytes()).hexdigest()

        def load():
            raise WithingsError("api_failed")

        failed = sync_withings(load, self.workbook, [DAY], RECORDED)
        self.assertEqual(failed["auth"], "api_failed")
        self.assertEqual(failed["errors"], 1)
        self.assertEqual(failed["measurements_appended"], 0)
        self.assertEqual(sha256(self.path.read_bytes()).hexdigest(), before)
        self.assertTrue(load_workbook(self.path).valid)
        self.assertNotIn("secret", json.dumps(failed))
