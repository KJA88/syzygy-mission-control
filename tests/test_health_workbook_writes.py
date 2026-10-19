"""Phase 2A Measurements writes. Synthetic rows only."""

import gc
import io
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from hashlib import sha256
from pathlib import Path

from health_workbook.mutate import add_measurements_sheet, package_parts
from health_workbook.service import Config, build_logger, serve
from health_workbook.store import WriteStore
from health_workbook.workbook import TABLE_HEADERS, load_workbook
from tests.test_health_workbook import TOKEN, fixture_sheets, write_workbook

MAINTAIN = "phase2a-maintain-token"


def measurement(**overrides):
    body = {
        "timestamp": "2026-10-01T07:10:00-07:00",
        "metric": "blood_glucose",
        "value": 103,
        "unit": "mg/dL",
        "source": "GLUCOSE_MONITOR",
        "device": "meter",
        "external_id": "glu-1",
        "context": "fasting",
        "updated_by": "fixture",
        "recorded_at": "2026-10-01T14:11:00Z",
    }
    body.update(overrides)
    return body


class MeasurementWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._cleanup_tmp)
        self.path = Path(self.tmp.name) / "staged.xlsx"
        write_workbook(self.path, fixture_sheets())
        original = self.path.read_bytes()
        self.path.write_bytes(add_measurements_sheet(original))
        self.daily_before = package_parts(self.path.read_bytes())["xl/worksheets/sheet4.xml"]
        self.logs = io.StringIO()
        self.config = Config(self.path, TOKEN, "127.0.0.1", 0, MAINTAIN)
        self.server = serve(self.config, build_logger(self.logs))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

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

    def request(self, method, path, payload=None, token=MAINTAIN, raw=None):
        data = raw if raw is not None else (None if payload is None else json.dumps(payload).encode())
        headers = {}
        if token:
            headers["Authorization"] = "Bearer " + token
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                body = json.loads(response.read().decode())
                return response.status, body
        except urllib.error.HTTPError as error:
            try:
                parsed = json.loads(error.read().decode())
            finally:
                error.close()
            return error.code, parsed

    def test_append_same_day_pair_and_idempotent_replay(self):
        before = self.path.read_bytes()
        status, body = self.request("POST", "/v1/measurements", measurement())
        self.assertEqual(status, 201)
        self.assertEqual(body["result"], "appended")
        self.assertEqual(body["row_id"], "measurements:2")
        self.assertTrue(body["backup"])
        backups = list((self.path.parent / "backups").glob("*.xlsx"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(sha256(backups[0].read_bytes()).digest(), sha256(before).digest())

        status, second = self.request("POST", "/v1/measurements", measurement(
            timestamp="2026-10-01T12:05:00-07:00",
            value=141,
            context="post_meal",
            external_id="glu-2",
        ))
        self.assertEqual(status, 201)
        self.assertEqual(second["row_id"], "measurements:3")
        status, replay = self.request("POST", "/v1/measurements", measurement())
        self.assertEqual(status, 200)
        self.assertEqual(replay["result"], "exists")
        self.assertEqual(replay["row_id"], "measurements:2")
        self.assertIsNone(replay["backup"])

        status, listed = self.request("GET", "/v1/rows?sheet=measurements&limit=10")
        self.assertEqual(listed["matched"], 2)
        self.assertEqual([row["external_id"] for row in listed["rows"]], ["glu-1", "glu-2"])
        self.assertEqual([row["value"] for row in listed["rows"]], [103, 141])
        self.assertEqual(self.daily_sheet(), self.daily_before)
        loaded = load_workbook(self.path)
        self.assertTrue(loaded.valid, loaded.detail)
        self.assertEqual(len(loaded.workbook.sheets["Daily"].rows), 3)

    def test_correction_keeps_the_previous_value_in_the_audit(self):
        self.request("POST", "/v1/measurements", measurement(value=103))
        status, body = self.request("PATCH", "/v1/measurements/measurements:2", {
            "source": "GLUCOSE_MONITOR",
            "updated_by": "fixture",
            "recorded_at": "2026-10-01T20:00:00Z",
            "reason": "meter memory showed 108",
            "fields": {"value": 108},
        })
        self.assertEqual(status, 200)
        self.assertEqual(body["result"], "updated")
        listed = self.request("GET", "/v1/sheets/measurements")[1]
        self.assertEqual(listed["rows"][0]["value"], 108)
        audit = WriteStore(self.path).audit_entries()
        correction = audit[-1]
        self.assertEqual(correction["action"], "correct")
        self.assertEqual(correction["reason"], "meter memory showed 108")
        self.assertEqual(json.loads(correction["before_json"])["value"], 103)
        self.assertEqual(json.loads(correction["after_json"])["value"], 108)
        self.assertEqual(self.daily_sheet(), self.daily_before)

    def _seed_marble_row(self):
        """A feeder-owned reading, as measurements:159-168 were before the Oct 9 edit."""
        status, body = self.request("POST", "/v1/measurements", measurement(
            metric="weight", value=204.3, unit="lb", source="WITHINGS", device="BODY_SMART",
            external_id="withings:1:weight", updated_by="withings-sync", recorded_at="2026-10-05T19:37:40Z",
        ))
        self.assertEqual(status, 201, body)
        return body["row_id"]

    def _row(self, row_id):
        return next(row for row in self.request("GET", "/v1/sheets/measurements")[1]["rows"] if row["row_id"] == row_id)

    def test_correction_by_another_writer_keeps_row_provenance(self):
        row_id = self._seed_marble_row()
        status, body = self.request("PATCH", "/v1/measurements/" + row_id, {
            "source": "USER", "updated_by": "Goggins", "recorded_at": "2026-10-09T20:36:00-07:00",
            "reason": "Flag Oct 5 marble-floor weigh-in as invalid per K JA",
            "fields": {"notes": "INVALID: marble floor"},
        })
        self.assertEqual(status, 200, body)
        row = self._row(row_id)
        self.assertEqual(row["source"], "WITHINGS")
        self.assertEqual(row["recorded_at"], "2026-10-05T19:37:40Z")
        self.assertEqual(row["updated_by"], "Goggins")
        self.assertEqual(row["notes"], "INVALID: marble floor")
        self.assertEqual(row["value"], 204.3)
        entry = WriteStore(self.path).audit_entries()[-1]
        self.assertEqual(entry["actor"], "Goggins")
        self.assertEqual(entry["source"], "USER")
        self.assertIn("row provenance kept: source, recorded_at", entry["reason"])
        self.assertIn("edit recorded_at=2026-10-09T20:36:00-07:00", entry["reason"])
        before, after = json.loads(entry["before_json"]), json.loads(entry["after_json"])
        self.assertEqual(before["source"], "WITHINGS")
        self.assertEqual(after["source"], "WITHINGS")
        self.assertEqual(after["recorded_at"], "2026-10-05T19:37:40Z")
        self.assertIsNone(before["notes"])
        # the Withings feeder still finds its row by (source, external_id): no duplicate identity
        status, replay = self.request("POST", "/v1/measurements", measurement(
            metric="weight", value=204.3, unit="lb", source="WITHINGS", device="BODY_SMART",
            external_id="withings:1:weight", updated_by="withings-sync", recorded_at="2026-10-05T19:37:40Z",
        ))
        self.assertIn(status, (200, 409), replay)
        self.assertEqual(replay.get("row_id"), row_id)

    def test_same_writer_correction_still_refreshes_recorded_at(self):
        row_id = self._seed_marble_row()
        status, body = self.request("PATCH", "/v1/measurements/" + row_id, {
            "source": "WITHINGS", "updated_by": "withings-sync", "recorded_at": "2026-10-06T10:00:00Z",
            "reason": "withings-sync", "fields": {"value": 204.4},
        })
        self.assertEqual(status, 200, body)
        row = self._row(row_id)
        self.assertEqual(row["recorded_at"], "2026-10-06T10:00:00Z")
        self.assertEqual(row["value"], 204.4)
        self.assertEqual(WriteStore(self.path).audit_entries()[-1]["reason"], "withings-sync")

    def test_changing_provenance_without_repair_reason_is_refused(self):
        row_id = self._seed_marble_row()
        before = self.path.read_bytes()
        status, body = self.request("PATCH", "/v1/measurements/" + row_id, {
            "source": "USER", "updated_by": "Goggins", "recorded_at": "2026-10-09T20:36:00-07:00",
            "reason": "relabel", "fields": {"source": "USER"},
        })
        self.assertEqual(body["error"], "provenance_locked", body)
        self.assertGreaterEqual(status, 400)
        self.assertEqual(self.path.read_bytes(), before)

    def test_provenance_repair_restores_metadata_only_and_is_not_skipped(self):
        row_id = self._seed_marble_row()
        # simulate the Oct 9 damage with the old behaviour: a repair-mode write that flips provenance
        self.request("PATCH", "/v1/measurements/" + row_id, {
            "source": "USER", "updated_by": "Goggins", "recorded_at": "2026-10-09T20:36:00-07:00",
            "reason": "provenance-repair: simulate damage", "fields": {"source": "USER", "recorded_at": "2026-10-09T20:36:00-07:00", "updated_by": "Goggins"},
        })
        damaged = self._row(row_id)
        self.assertEqual((damaged["source"], damaged["updated_by"]), ("USER", "Goggins"))
        status, body = self.request("PATCH", "/v1/measurements/" + row_id, {
            "source": "WITHINGS", "updated_by": "grok-bot", "recorded_at": "2026-10-05T19:37:40Z",
            "reason": "provenance-repair: restore original Withings provenance (audit 522)",
            "fields": {"source": "WITHINGS", "recorded_at": "2026-10-05T19:37:40Z"},
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(body["result"], "updated")
        row = self._row(row_id)
        self.assertEqual(row["source"], "WITHINGS")
        self.assertEqual(row["recorded_at"], "2026-10-05T19:37:40Z")
        self.assertEqual(row["updated_by"], "Goggins")
        self.assertEqual(row["value"], 204.3)
        entry = WriteStore(self.path).audit_entries()[-1]
        self.assertEqual(entry["actor"], "grok-bot")
        self.assertEqual(json.loads(entry["before_json"])["source"], "USER")
        self.assertEqual(json.loads(entry["after_json"])["source"], "WITHINGS")
        self.assertEqual(json.loads(entry["after_json"])["updated_by"], "Goggins")

    def test_provenance_repair_refuses_data_fields(self):
        row_id = self._seed_marble_row()
        before = self.path.read_bytes()
        status, body = self.request("PATCH", "/v1/measurements/" + row_id, {
            "source": "WITHINGS", "updated_by": "grok-bot", "recorded_at": "2026-10-05T19:37:40Z",
            "reason": "provenance-repair: sneak a value", "fields": {"source": "WITHINGS", "value": 1},
        })
        self.assertEqual(body["error"], "repair_fields_only", body)
        self.assertEqual(self.path.read_bytes(), before)

    def test_read_token_and_anonymous_writes_are_rejected(self):
        before = self.path.read_bytes()
        status, body = self.request("POST", "/v1/measurements", measurement(), token=None)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")
        status, body = self.request("POST", "/v1/measurements", measurement(), token=TOKEN)
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "forbidden")
        status, body = self.request("PATCH", "/v1/measurements/measurements:2", {"reason": "no"}, token=TOKEN)
        self.assertEqual(status, 403)
        status, body = self.request("POST", "/v1/weight-trend", measurement())
        self.assertEqual(status, 405)
        self.assertEqual(body["error"], "writes_disabled")
        self.assertEqual(body["writes_enabled"], ["measurements", "daily", "meals", "training", "lifts", "notes"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_malformed_request_does_not_change_the_workbook(self):
        before = self.path.read_bytes()
        status, body = self.request("POST", "/v1/measurements", measurement(metric=""))
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "missing_field")
        status, body = self.request("POST", "/v1/measurements", raw=b"{")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "malformed")
        status, body = self.request("PATCH", "/v1/measurements/measurements:2", {
            "source": "GLUCOSE_MONITOR",
            "updated_by": "fixture",
            "recorded_at": "2026-10-01T20:00:00Z",
            "fields": {"value": 108},
        })
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "missing_field")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.path.parent / "backups").exists())

    def test_failed_write_rolls_back(self):
        before = self.path.read_bytes()
        for fault in ("before_replace", "after_replace", "audit"):
            failed = Config(self.path, TOKEN, maintain_token=MAINTAIN, fault=fault)
            from health_workbook.service import App
            status, body = App(failed, build_logger(io.StringIO())).dispatch(
                "POST",
                "/v1/measurements",
                {"Authorization": "Bearer " + MAINTAIN},
                json.dumps(measurement(external_id="fail-" + fault)).encode(),
            )
            self.assertEqual(status, 500, fault)
            self.assertEqual(body["error"], "write_failed")
            self.assertEqual(self.path.read_bytes(), before, fault)
        self.assertEqual(WriteStore(self.path).audit_entries(), [])

    def test_concurrent_appends_serialize(self):
        errors = []

        def send(index):
            try:
                status, body = self.request("POST", "/v1/measurements", measurement(
                    external_id=f"batch-{index}",
                    value=100 + index,
                ))
                if status != 201:
                    errors.append(body)
            except Exception as error:
                errors.append(repr(error))

        threads = [threading.Thread(target=send, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        listed = self.request("GET", "/v1/rows?sheet=measurements&limit=20")[1]
        self.assertEqual(listed["matched"], 8)
        self.assertEqual(len({row["external_id"] for row in listed["rows"]}), 8)

        def replay():
            status, _body = self.request("POST", "/v1/measurements", measurement(external_id="same-id", value=5))
            if status not in {200, 201}:
                errors.append(status)

        threads = [threading.Thread(target=replay) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        rows = self.request("GET", "/v1/rows?sheet=measurements&limit=20")[1]["rows"]
        self.assertEqual(sum(1 for row in rows if row["external_id"] == "same-id"), 1)
        self.assertEqual(self.daily_sheet(), self.daily_before)
        self.assertTrue(load_workbook(self.path).valid)

    def test_logs_omit_measurement_values(self):
        self.request("POST", "/v1/measurements", measurement(metric="marker-glucose"))
        self.assertNotIn("marker-glucose", self.logs.getvalue())
        self.assertNotIn(MAINTAIN, self.logs.getvalue())

    def test_writable_sheets_append_conflict_and_correct(self):
        cases = {
            "daily": (
                {"date": "2026-02-01", "steps": 1},
                {"date": "2026-02-02", "steps": 2},
                {"steps": 9},
            ),
            "meals": (
                {"date": "2026-02-01", "time": "08:00", "food": "syzygy-test-oats", "kcal": 10},
                {"date": "2026-02-01", "time": "12:00", "food": "syzygy-test-beans", "kcal": 20},
                {"kcal": 30},
            ),
            "training": (
                {"date": "2026-02-01", "session_id": "SMOKE", "source": "FITBIT", "type": "walk", "burn_role": "do_not_sum"},
                {"date": "2026-02-01", "session_id": "SMOKE", "source": "POLAR_H10", "type": "walk", "burn_role": "session_hr_burn"},
                {"calories": 5},
            ),
            "lifts": (
                {"date": "2026-02-01", "exercise": "syzygy-test-row", "reps_set1": 5, "weight_set1": 10},
                {"date": "2026-02-02", "exercise": "syzygy-test-row", "reps_set1": 5, "weight_set1": 10},
                {"weight_set1": 15},
            ),
            "notes": (
                {"datetime": "2026-02-01 08:00", "category": "test", "note_paraphrased": "syzygy-test-a"},
                {"datetime": "2026-02-01 09:00", "category": "test", "note_paraphrased": "syzygy-test-b"},
                {"severity": "mild"},
            ),
        }
        for sheet, (first, second, correction) in cases.items():
            with self.subTest(sheet=sheet):
                before_parts = package_parts(self.path.read_bytes())
                body = self._attributed(first)
                status, created = self.request("POST", "/v1/" + sheet, body)
                self.assertEqual(status, 201, created)
                self.assertEqual(created["result"], "appended")
                self.assertTrue(created["backup"])
                status, replay = self.request("POST", "/v1/" + sheet, body)
                self.assertEqual(status, 200, replay)
                self.assertEqual(replay["result"], "exists")
                self.assertEqual(replay["row_id"], created["row_id"])
                status, other = self.request("POST", "/v1/" + sheet, self._attributed(second))
                self.assertEqual(status, 201, other)
                self.assertNotEqual(other["row_id"], created["row_id"])
                changed = dict(body)
                changed.update(correction)
                status, conflict = self.request("POST", "/v1/" + sheet, changed)
                self.assertEqual(status, 409, conflict)
                self.assertEqual(conflict["error"], "conflict")
                status, patched = self.request("PATCH", "/v1/" + sheet + "/" + created["row_id"], {
                    "source": body["source"],
                    "updated_by": "fixture",
                    "recorded_at": "2026-02-01T12:00:00Z",
                    "reason": "syzygy test correction",
                    "fields": correction,
                })
                self.assertEqual(status, 200, patched)
                audit = WriteStore(self.path).audit_entries()
                self.assertEqual(audit[-1]["action"], "correct")
                self.assertEqual(audit[-1]["sheet"], created["sheet"])
                self.assertIsNotNone(audit[-1]["before_json"])
                after_parts = package_parts(self.path.read_bytes())
                for name in (
                    "xl/worksheets/sheet1.xml",
                    "xl/worksheets/sheet2.xml",
                    "xl/worksheets/sheet3.xml",
                    "xl/worksheets/sheet9.xml",
                ):
                    self.assertEqual(before_parts[name], after_parts[name], name)
                self.assertTrue(load_workbook(self.path).valid)

    def test_training_devices_stay_separate_and_daily_stays_one_row(self):
        first = self._attributed({
            "date": "2026-03-01", "session_id": "SHARED", "source": "FITBIT", "type": "walk", "burn_role": "do_not_sum",
        })
        second = self._attributed({
            "date": "2026-03-01", "session_id": "SHARED", "source": "POLAR_H10", "type": "walk", "burn_role": "session_hr_burn",
        })
        self.assertEqual(self.request("POST", "/v1/training", first)[0], 201)
        self.assertEqual(self.request("POST", "/v1/training", second)[0], 201)
        rows = self.request("GET", "/v1/rows?sheet=training&session_id=SHARED&limit=10")[1]["rows"]
        self.assertEqual(sorted(row["source"] for row in rows if row["session_id"] == "SHARED"), ["FITBIT", "POLAR_H10"])
        day = self._attributed({"date": "2026-03-02", "steps": 4, "source": "FITBIT"})
        self.assertEqual(self.request("POST", "/v1/daily", day)[0], 201)
        other = self._attributed({"date": "2026-03-02", "steps": 8, "source": "SCALE"})
        status, body = self.request("POST", "/v1/daily", other)
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "conflict")
        listed = self.request("GET", "/v1/rows?sheet=daily&date=2026-03-02&limit=10")[1]
        self.assertEqual(listed["matched"], 1)

    def test_measurements_migration_is_idempotent(self):
        bare = Path(self.tmp.name) / "bare.xlsx"
        write_workbook(bare, fixture_sheets())
        original = package_parts(bare.read_bytes())
        first = WriteStore(bare).migrate_measurements()
        self.assertEqual(first["result"], "migrated")
        self.assertTrue(first["backup"])
        added = package_parts(bare.read_bytes())
        for name, payload in original.items():
            if name in {"xl/workbook.xml", "xl/_rels/workbook.xml.rels", "[Content_Types].xml"}:
                continue
            self.assertEqual(added.get(name), payload)
        self.assertIn("xl/worksheets/sheet9.xml", added)
        unchanged = bare.read_bytes()
        second = WriteStore(bare).migrate_measurements()
        self.assertEqual(second["result"], "present")
        self.assertIsNone(second["backup"])
        self.assertEqual(bare.read_bytes(), unchanged)
        self.assertEqual(
            load_workbook(bare).workbook.sheets["Measurements"].headers,
            list(TABLE_HEADERS["Measurements"]),
        )
        self.assertEqual(add_measurements_sheet.__name__, "add_measurements_sheet")

    def test_training_correction_keeps_source_and_repair_restores_it(self):
        body = self._attributed({"date": "2026-10-06", "session_id": "2026-10-06-treadmill-console", "source": "MANUAL",
                                 "type": "treadmill incline walk", "start_local": "~11:41 AM", "updated_by": "Goggins"})
        status, created = self.request("POST", "/v1/training", body)
        self.assertEqual(status, 201, created)
        row_id = created["row_id"]
        status, patched = self.request("PATCH", "/v1/training/" + row_id, {
            "source": "USER", "updated_by": "Goggins", "recorded_at": "2026-10-09T20:26:00-07:00",
            "reason": "Correct start time", "fields": {"start_local": "11:39 AM"},
        })
        self.assertEqual(status, 200, patched)
        row = next(r for r in self.request("GET", "/v1/rows?sheet=training&date=2026-10-06&limit=5")[1]["rows"] if r["row_id"] == row_id)
        self.assertEqual(row["source"], "MANUAL")
        self.assertEqual(row["start_local"], "11:39 AM")
        status, repaired = self.request("PATCH", "/v1/training/" + row_id, {
            "source": "MANUAL", "updated_by": "grok-bot", "recorded_at": "2026-10-06T12:43:00-07:00",
            "reason": "provenance-repair: no-op check", "fields": {"source": "MANUAL"},
        })
        self.assertEqual(status, 200, repaired)
        row = next(r for r in self.request("GET", "/v1/rows?sheet=training&date=2026-10-06&limit=5")[1]["rows"] if r["row_id"] == row_id)
        self.assertEqual((row["source"], row["updated_by"]), ("MANUAL", "Goggins"))

    def test_public_row_id_updates_without_duplicating_the_identity(self):
        body = self._attributed({"date": "2026-04-01", "steps": 100, "hrv_ms": 40, "source": "FITBIT"})
        status, created = self.request("POST", "/v1/daily", body)
        self.assertEqual(status, 201, created)
        row_id = created["row_id"]
        number = row_id.split(":", 1)[1]
        listed = self.request("GET", "/v1/rows?sheet=daily&date=2026-04-01&limit=5")[1]
        self.assertEqual(listed["matched"], 1)
        self.assertEqual(listed["rows"][0]["row_id"], row_id)
        patch = {
            "source": "SYZYGY_TEST",
            "updated_by": "fixture",
            "recorded_at": "2026-04-01T01:00:00Z",
            "reason": "test patch",
            "fields": {"hrv_ms": 22.3},
        }
        for token in (row_id, "Daily:" + number, number, "daily%3A" + number):
            status, patched = self.request("PATCH", "/v1/daily/" + token, patch)
            self.assertEqual(status, 200, patched)
            self.assertEqual(patched["result"], "updated")
            self.assertEqual(patched["row_id"], row_id)
        after = self.request("GET", "/v1/rows?sheet=daily&date=2026-04-01&limit=5")[1]
        self.assertEqual(after["matched"], 1)
        self.assertEqual(after["rows"][0]["steps"], 100)
        self.assertEqual(after["rows"][0]["hrv_ms"], 22.3)
        self.assertEqual(after["rows"][0]["date"], "2026-04-01")
        changed = dict(body)
        changed["steps"] = 999
        status, conflict = self.request("POST", "/v1/daily", changed)
        self.assertEqual(status, 409, conflict)
        self.assertEqual(conflict["error"], "conflict")
        self.assertEqual(conflict["row_id"], row_id)
        self.assertEqual(self.request("GET", "/v1/rows?sheet=daily&date=2026-04-01&limit=5")[1]["matched"], 1)
        self.assertEqual(self.request("PATCH", "/v1/daily/not-a-row", patch)[1]["error"], "row_missing")
        self.assertEqual(self.request("PATCH", "/v1/daily/meals:" + number, patch)[1]["error"], "row_missing")
        self.assertEqual(self.request("PATCH", "/v1/daily/daily:1", patch)[1]["error"], "row_missing")
        updates = [
            row for row in WriteStore(self.path).audit_entries()
            if row["action"] == "correct" and row["row_id"] == row_id
        ]
        self.assertGreaterEqual(len(updates), 1)
        before = json.loads(updates[0]["before_json"])
        written = json.loads(updates[0]["after_json"])
        self.assertEqual(updates[0]["reason"], "test patch")
        self.assertEqual(before.get("hrv_ms"), 40)
        self.assertEqual(before["steps"], 100)
        self.assertEqual(written["hrv_ms"], 22.3)
        self.assertEqual(written["steps"], 100)
        cleared = dict(patch)
        cleared["fields"] = {"hrv_ms": None}
        cleared["reason"] = "revert test patch"
        status, reverted = self.request("PATCH", "/v1/daily/" + row_id, cleared)
        self.assertEqual(status, 200, reverted)
        restored = self.request("GET", "/v1/rows?sheet=daily&date=2026-04-01&limit=5")[1]["rows"][0]
        self.assertIsNone(restored["hrv_ms"])
        self.assertEqual(restored["steps"], 100)
        self.assertEqual(restored["row_id"], row_id)

    def _attributed(self, values):
        body = {
            "source": values.get("source", "SYZYGY_TEST"),
            "updated_by": "fixture",
            "recorded_at": "2026-02-01T00:00:00Z",
        }
        body.update(values)
        return body

    def daily_sheet(self):
        return package_parts(self.path.read_bytes())["xl/worksheets/sheet4.xml"]
