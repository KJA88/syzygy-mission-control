"""Fitbit and Macro App feeders. Synthetic rows only."""

import gc
import sqlite3
import tempfile
import threading
import time
import unittest
from hashlib import sha256
from pathlib import Path

from health_workbook.feeders import (
    WorkbookClient,
    fitbit_daily_fields,
    load_macro_entries,
    macro_totals,
    sync_fitbit,
    sync_macro,
)
from health_workbook.service import Config, build_logger, serve
from health_workbook.workbook import load_workbook
from tests.test_health_workbook import TOKEN, fixture_sheets, write_workbook

MAINTAIN = "phase2a-maintain-token"
RECORDED = "2026-01-05T18:00:00Z"
DAY = "2026-01-05"


def _workout(calories=120):
    return {
        "exercise": "Walk",
        "type": "WALKING",
        "start": "2026-01-05T15:00:00-08:00",
        "end": "2026-01-05T15:30:00-08:00",
        "duration_minutes": 30,
        "calories": calories,
        "distance_miles": 2,
        "average_heart_rate_bpm": 110,
    }


def _snapshots(steps=4321, raw_points=None, calories=120, include_steps=True, day=DAY):
    points = [{}] if raw_points is None else raw_points
    steps_row = {"date": day, "steps": steps, "raw_points": points}
    return {
        "get_fitbit_steps_history": {"records": [steps_row]} if include_steps else None,
        "get_fitbit_distance_history": {"records": [{"date": day, "distance_miles": 3}]},
        "get_fitbit_active_zone_minutes_history": {"records": [{"date": day, "active_zone_minutes": 12}]},
        "get_fitbit_total_calories_history": {"records": [{"date": day, "total_calories_kcal": 2100}]},
        "get_fitbit_active_energy_burned_history": {"records": [{"date": day, "active_energy_kcal": 400}]},
        "get_fitbit_hrv_history": {"records": [{"date": day, "average_hrv_ms": 41}]},
        "get_fitbit_resting_heart_rate_history": {"records": [{"date": day, "resting_heart_rate_bpm": 58}]},
        "get_fitbit_sleep_history": {"records": [
            {"local_wake_date": day, "main_sleep": False, "minutes_asleep": 20},
            {"local_wake_date": day, "main_sleep": True, "minutes_asleep": 390},
        ]},
        "get_fitbit_weight_history": {"records": [{"date": day, "weight_pounds": 180, "physical_time": "t"}]},
        "get_fitbit_exercise_history": [_workout(calories)],
    }


class FeederPlanningTests(unittest.TestCase):
    def test_resting_burn_and_missing_metric(self):
        fields = fitbit_daily_fields(_snapshots(), DAY)
        self.assertEqual(fields["fitbit_resting_burn"], 1700)
        self.assertEqual(fields["sleep_asleep_min"], 390)
        self.assertNotIn("sleep_score", fields)
        self.assertNotIn("readiness", fields)
        missing = fitbit_daily_fields(_snapshots(raw_points=[]), DAY)
        self.assertNotIn("steps", missing)
        no_active = _snapshots()
        no_active["get_fitbit_active_energy_burned_history"] = None
        self.assertNotIn("fitbit_resting_burn", fitbit_daily_fields(no_active, DAY))

    def test_macro_totals_skip_covered_and_supplements(self):
        entries = [
            {"entry_date": DAY, "nutrition_accounting": "aggregate", "category": "food", "kcal": 500, "protein_g": 40, "carbs_g": 50, "fat_g": 10},
            {"entry_date": DAY, "nutrition_accounting": "covered_by_aggregate", "category": "food", "kcal": 100, "protein_g": 1, "carbs_g": 1, "fat_g": 1},
            {"entry_date": DAY, "nutrition_accounting": "itemized", "category": "supplement", "kcal": 5, "protein_g": 0, "carbs_g": 0, "fat_g": 0},
        ]
        self.assertEqual(macro_totals(entries, DAY)["kcal_in"], 500)


class FeederServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._cleanup_tmp)
        self.path = Path(self.tmp.name) / "staged.xlsx"
        write_workbook(self.path, fixture_sheets())
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

    def _fetch(self, snapshots):
        def fetch(name, _dates):
            if name not in snapshots or snapshots[name] is None and name.startswith("missing-"):
                raise RuntimeError("down")
            return snapshots.get(name)
        return fetch

    def _row(self, sheet, **filters):
        rows = self.workbook.rows(sheet, **filters)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def _same(self, actual, expected):
        if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            self.assertEqual(float(actual), float(expected))
        else:
            self.assertEqual(actual, expected)

    def test_fitbit_daily_sync_and_replay(self):
        result = sync_fitbit(self._fetch(_snapshots()), self.workbook, [DAY], RECORDED)
        self.assertEqual(result["daily_appended"], 1)
        self.assertEqual(result["errors"], 0)
        self.assertIn("steps", result["fields_written"])
        self.assertIn("fitbit_resting_burn", result["fields_written"])
        row = self._row("daily", date=DAY)
        self._same(row["steps"], 4321)
        self._same(row["fitbit_resting_burn"], 1700)
        self.assertIsNone(row["kcal_in"])
        self.assertIsNone(row["vodka_g"])
        self.assertIsNone(row["sleep_score"])
        self.assertIsNone(row["notes"])
        digest = sha256(self.path.read_bytes()).hexdigest()
        again = sync_fitbit(self._fetch(_snapshots()), self.workbook, [DAY], RECORDED)
        self.assertEqual(again["daily_exists"], 1)
        self.assertEqual(again["daily_appended"], 0)
        self.assertEqual(again["daily_patched"], 0)
        self.assertEqual(sha256(self.path.read_bytes()).hexdigest(), digest)

    def test_fitbit_training_dedupe(self):
        snapshots = _snapshots()
        first = sync_fitbit(self._fetch(snapshots), self.workbook, [DAY], RECORDED)
        self.assertEqual(first["workouts_appended"], 1)
        digest = sha256(self.path.read_bytes()).hexdigest()
        second = sync_fitbit(self._fetch(snapshots), self.workbook, [DAY], RECORDED)
        self.assertEqual(second["workouts_exists"], 1)
        self.assertEqual(second["workouts_appended"], 0)
        self.assertEqual(sha256(self.path.read_bytes()).hexdigest(), digest)
        fitbit_rows = [row for row in self.workbook.rows("training", date=DAY) if str(row.get("session_id", "")).startswith("fitbit:")]
        self.assertEqual(len(fitbit_rows), 1)
        self.assertEqual(fitbit_rows[0]["burn_role"], "do_not_sum")
        self.assertEqual(fitbit_rows[0]["updated_by"], "fitbit-sync")

    def test_training_conflict_and_weight_conflict(self):
        sync_fitbit(self._fetch(_snapshots()), self.workbook, [DAY], RECORDED)
        workout = [
            row for row in self.workbook.rows("training", date=DAY) if str(row.get("session_id", "")).startswith("fitbit:")
        ][0]
        self.workbook.patch("training", workout["row_id"], {"calories": 1}, "coach edit", "FITBIT", "coach", RECORDED)
        changed = sync_fitbit(self._fetch(_snapshots(calories=80)), self.workbook, [DAY], RECORDED)
        self.assertEqual(changed["workouts_conflicts"], 1)
        self.assertEqual(changed["workouts_patched"], 0)
        current = [row for row in self.workbook.rows("training", date=DAY) if str(row.get("session_id", "")).startswith("fitbit:")][0]
        self._same(current["calories"], 1)
        manual = self._row("daily", date="2026-01-02")
        self.workbook.patch("daily", manual["row_id"], {"weight_lb": 200}, "manual weight", "SCALE", "fixture", RECORDED)
        weighed = sync_fitbit(self._fetch(_snapshots(day="2026-01-02")), self.workbook, ["2026-01-02"], RECORDED)
        self.assertIn("2026-01-02:weight_lb", weighed["conflicts"])
        kept = self._row("daily", date="2026-01-02")
        self._same(kept["weight_lb"], 200)
        self._same(kept["steps"], 4321)
        meal = self._row("meals", date="2026-01-02")
        self._same(meal["kcal"], 100)

    def test_macro_meals_totals_and_replay(self):
        entries = [{
            "id": "meal-1",
            "entry_date": DAY,
            "logged_time": "12:00",
            "food_name_snapshot": "yogurt",
            "kcal": 200,
            "protein_g": 20,
            "carbs_g": 15,
            "fat_g": 6,
            "category": "food",
            "nutrition_accounting": "itemized",
        }]
        result = sync_macro(entries, self.workbook, [DAY], RECORDED)
        self.assertEqual(result["meals_appended"], 1)
        self.assertEqual(result["daily_appended"], 1)
        self.assertEqual(result["daily_totals_written"], ["carbs_g", "fat_g", "kcal_in", "protein_g"])
        row = self._row("daily", date=DAY)
        self._same(row["kcal_in"], 200)
        self._same(row["protein_g"], 20)
        self.assertIsNone(row["steps"])
        digest = sha256(self.path.read_bytes()).hexdigest()
        again = sync_macro(entries, self.workbook, [DAY], RECORDED)
        self.assertEqual(again["meals_exists"], 1)
        self.assertEqual(again["daily_exists"], 1)
        self.assertEqual(again["meals_appended"], 0)
        self.assertEqual(sha256(self.path.read_bytes()).hexdigest(), digest)

    def test_macro_does_not_overwrite_manual_meal(self):
        entries = [{
            "id": "meal-2",
            "entry_date": "2026-01-02",
            "logged_time": "08:00",
            "food_name_snapshot": "fixture-oats",
            "kcal": 250,
            "protein_g": 30,
            "carbs_g": 40,
            "fat_g": 8,
            "category": "food",
            "nutrition_accounting": "itemized",
        }]
        result = sync_macro(entries, self.workbook, ["2026-01-02"], RECORDED)
        self.assertEqual(result["meals_conflicts"], 1)
        self.assertEqual(result["meals_patched"], 0)
        meal = self._row("meals", date="2026-01-02")
        self._same(meal["kcal"], 100)
        self.assertEqual(meal["updated_by"], "fixture")

    def test_missing_fitbit_metric(self):
        result = sync_fitbit(self._fetch(_snapshots(raw_points=[])), self.workbook, [DAY], RECORDED)
        self.assertNotIn("steps", result["fields_written"])
        self.assertIn("distance_mi", result["fields_written"])
        row = self._row("daily", date=DAY)
        self.assertIsNone(row["steps"])
        self._same(row["distance_mi"], 3)

    def test_fitbit_failure_leaves_workbook_valid_and_macro_still_runs(self):
        before = sha256(self.path.read_bytes()).hexdigest()

        def fail(_name, _dates):
            raise RuntimeError("mcp down")

        failed = sync_fitbit(fail, self.workbook, [DAY], RECORDED)
        self.assertGreater(failed["errors"], 0)
        self.assertEqual(failed["daily_appended"], 0)
        self.assertEqual(failed["workouts_appended"], 0)
        self.assertEqual(sha256(self.path.read_bytes()).hexdigest(), before)
        entries = [{
            "id": "meal-3",
            "entry_date": DAY,
            "logged_time": "13:00",
            "food_name_snapshot": "beans",
            "kcal": 80,
            "protein_g": 6,
            "carbs_g": 10,
            "fat_g": 1,
            "category": "food",
            "nutrition_accounting": "itemized",
        }]
        synced = sync_macro(entries, self.workbook, [DAY], RECORDED)
        self.assertEqual(synced["meals_appended"], 1)
        self.assertEqual(synced["errors"], 0)
        loaded = load_workbook(self.path)
        self.assertTrue(loaded.valid)

    def test_macro_sqlite_read(self):
        database = Path(self.tmp.name) / "macros.sqlite3"
        connection = sqlite3.connect(database)
        connection.executescript("""
            CREATE TABLE food_entries (
                id TEXT, entry_date TEXT, logged_time TEXT, food_name_snapshot TEXT,
                kcal REAL, protein_g REAL, carbs_g REAL, fat_g REAL, category TEXT,
                created_at TEXT
            );
            CREATE TABLE imported_entry_details (
                entry_id TEXT, nutrition_accounting TEXT
            );
        """)
        connection.execute(
            "INSERT INTO food_entries VALUES ('a', ?, '07:00', 'apple', 90, 1, 20, 0, 'food', 't')",
            (DAY,),
        )
        connection.commit()
        connection.close()
        rows = load_macro_entries(str(database), [DAY])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["nutrition_accounting"], "itemized")
        self.assertEqual(rows[0]["food_name_snapshot"], "apple")
