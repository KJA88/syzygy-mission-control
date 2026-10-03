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
from health_workbook.workbook import load_workbook
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
        self.assertEqual(body["result"], "corrected")
        listed = self.request("GET", "/v1/sheets/measurements")[1]
        self.assertEqual(listed["rows"][0]["value"], 108)
        audit = WriteStore(self.path).audit_entries()
        correction = audit[-1]
        self.assertEqual(correction["action"], "correct")
        self.assertEqual(correction["reason"], "meter memory showed 108")
        self.assertEqual(json.loads(correction["before_json"])["value"], 103)
        self.assertEqual(json.loads(correction["after_json"])["value"], 108)
        self.assertEqual(self.daily_sheet(), self.daily_before)

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
        status, body = self.request("POST", "/v1/meals", measurement())
        self.assertEqual(status, 405)
        self.assertEqual(body["error"], "writes_disabled")
        self.assertEqual(body["writes_enabled"], ["measurements"])
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

    def daily_sheet(self):
        return package_parts(self.path.read_bytes())["xl/worksheets/sheet4.xml"]
