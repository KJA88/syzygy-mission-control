"""Polar H10 parsing and workbook mapping. No radio and no live health data."""
import asyncio
import contextlib
import gc
import io
import json
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from hashlib import sha256

from health_workbook.feeders import WorkbookClient, zone
from health_workbook.mutate import add_measurements_sheet
from health_workbook.polar_h10 import (
    advertisement_matches_h10,
    collect_samples,
    request_stop,
    describe_h10,
    hrv_from_intervals,
    parse_heart_rate_measurement,
    mark_partial_capture,
    mark_test_capture,
    reportable_polar_workout,
    resting_measurements,
    seconds_limit,
    select_h10,
    sync_polar,
    workout_record,
    main,
    HR_MEASUREMENT_UUID,
    HR_SERVICE_UUID,
)
from health_workbook.service import Config, build_logger, serve
from tests.test_health_workbook import TOKEN, fixture_sheets, write_workbook

MAINTAIN = "phase2a-maintain-token"
RECORDED = "2026-10-03T18:00:00Z"
STARTED = datetime(2026, 10, 3, 7, 30, tzinfo=zone())


def _packet(heart_rate, raw_intervals):
    body = bytes([0x10, heart_rate])
    for raw in raw_intervals:
        body += int(raw).to_bytes(2, "little")
    return body


class PolarParseTests(unittest.TestCase):
    def test_raw_960_is_937_point_5_ms(self):
        heart_rate, intervals = parse_heart_rate_measurement(bytes([0x10, 63, 0xC0, 0x03]))
        self.assertEqual(heart_rate, 63)
        self.assertEqual(intervals, [937.5])

    def test_every_rr_in_one_notification_is_kept(self):
        heart_rate, intervals = parse_heart_rate_measurement(_packet(70, (1024, 512)))
        self.assertEqual(heart_rate, 70)
        self.assertEqual(intervals, [1000.0, 500.0])

    def test_uint16_heart_rate_and_energy_do_not_shift_rr(self):
        packet = bytes([0x19, 0x2C, 0x01, 0x34, 0x12, 0xC0, 0x03])
        heart_rate, intervals = parse_heart_rate_measurement(packet)
        self.assertEqual(heart_rate, 300)
        self.assertEqual(intervals, [937.5])

    def test_missing_rr_flag_is_heart_rate_only(self):
        self.assertEqual(parse_heart_rate_measurement(bytes([0x00, 63])), (63, []))

    def test_truncated_rr_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_heart_rate_measurement(bytes([0x10, 63, 0xC0]))

    def test_rmssd_uses_successive_accepted_intervals_only(self):
        stats = hrv_from_intervals([800, 100, 820, 840])
        self.assertEqual(stats["rejected"], 1)
        self.assertEqual(stats["accepted"], [800, 820, 840])
        self.assertEqual(stats["rmssd_ms"], 20)

    def test_discovery_reports_only_the_h10(self):
        polar = type("Device", (), {"name": "Polar H10 12345678", "address": "AA:BB:CC:DD:EE:FF"})()
        phone = type("Device", (), {"name": "Phone", "address": "11:22:33:44:55:66"})()
        records = [
            (phone, "Phone", None, [], phone.address),
            (polar, None, "Polar H10 12345678", [], polar.address),
        ]
        chosen, count = select_h10([(item[0], item[1], item[2], item[3]) for item in records])
        self.assertIs(chosen, polar)
        self.assertEqual(count, 1)
        report = describe_h10(records)
        encoded = json.dumps(report)
        self.assertEqual(report["name"], "Polar H10 12345678")
        self.assertEqual(report["address"], "AA:BB:CC:DD:EE:FF")
        self.assertNotIn("Phone", encoded)
        self.assertNotIn("11:22:33:44:55:66", encoded)
        self.assertIsNone(advertisement_matches_h10("Other strap", None, []))
        service_only = type("Device", (), {"address": "FE:ED:FA:CE:00:01"})()
        matched, service_count = select_h10([
            (service_only, None, None, ["0000180D-0000-1000-8000-00805F9B34FB"]),
        ])
        self.assertIs(matched, service_only)
        self.assertEqual(service_count, 1)
        named = object()
        winner, _count = select_h10([
            (service_only, None, None, [HR_SERVICE_UUID]),
            (named, "Polar H10", None, []),
        ])
        self.assertIs(winner, named)


class _NotifyClient:
    def __init__(self, device, packets, drop_after=None):
        self.device = device
        self.packets = packets
        self.drop_after = drop_after
        self.is_connected = True
        self.stopped = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def start_notify(self, uuid, callback):
        self.uuid = uuid
        for index, packet in enumerate(self.packets):
            callback(None, packet)
            if self.drop_after is not None and index + 1 >= self.drop_after:
                self.is_connected = False
                return

    async def stop_notify(self, uuid):
        self.stopped = uuid


class PolarReconnectTests(unittest.TestCase):
    def test_disconnect_rediscovers_once_and_keeps_both_samples(self):
        devices = iter(("first", "second"))
        opened = []

        async def find_device():
            return next(devices)

        def client_factory(device):
            opened.append(device)
            if device == "first":
                return _NotifyClient(device, [_packet(60, (1024,))], drop_after=1)
            return _NotifyClient(device, [_packet(62, (1040,))])

        async def sleep(_seconds):
            return None

        captured = asyncio.run(collect_samples(0.2, find_device, client_factory, sleep))
        self.assertTrue(captured["found"])
        self.assertEqual(captured["reconnects"], 1)
        self.assertEqual([sample[0] for sample in captured["samples"]], [60, 62])
        self.assertEqual(opened, ["first", "second"])

    def test_stop_request_ends_the_listen_and_keeps_samples(self):
        async def find_device():
            return "strap"

        async def sleep(_seconds):
            request_stop()

        captured = asyncio.run(collect_samples(
            30,
            find_device,
            lambda device: _NotifyClient(device, [_packet(76, (800,))]),
            sleep,
        ))
        self.assertTrue(captured["found"])
        self.assertEqual(captured["samples"], [(76, [781.25])])
        self.assertLess(captured["elapsed_s"], 5)

    def test_absent_device_does_not_raise(self):
        async def find_device():
            return None

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            captured = asyncio.run(collect_samples(5, find_device, lambda device: None))
        self.assertFalse(captured["found"])
        self.assertIsNone(captured["samples"])
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_connect_timeout_retries_discovery_once(self):
        calls = {"find": 0}

        async def find_device():
            calls["find"] += 1
            return "strap"

        class TimingOut:
            def __init__(self, device):
                self.device = device

            async def __aenter__(self):
                raise asyncio.TimeoutError()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        captured = asyncio.run(collect_samples(5, find_device, TimingOut))
        self.assertEqual(calls["find"], 2)
        self.assertEqual(captured["reconnects"], 1)
        self.assertEqual(captured["samples"], [])


class PolarWorkbookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = __import__("pathlib").Path(self.tmp.name) / "book.xlsx"
        write_workbook(self.path, fixture_sheets())
        self.path.write_bytes(add_measurements_sheet(self.path.read_bytes()))
        self.server = serve(Config(self.path, TOKEN, "127.0.0.1", 0, MAINTAIN), build_logger())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.workbook = WorkbookClient(f"http://127.0.0.1:{self.server.server_address[1]}", MAINTAIN)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        gc.collect()
        for _ in range(20):
            try:
                self.tmp.cleanup()
                return
            except PermissionError:
                time.sleep(0.05)
        self.tmp.cleanup()

    def _samples(self):
        return [(60, [800.0, 820.0]), (62, [840.0])]

    def test_resting_rows_replay_and_leave_fitbit_hrv_alone(self):
        day = STARTED.date().isoformat()
        daily = self.workbook.rows("daily", date="2026-01-02")[0]
        self.workbook.patch("daily", daily["row_id"], {"hrv_ms": 41}, "fitbit overnight", "FITBIT", "fitbit-sync", RECORDED)
        first = sync_polar("resting", self._samples(), STARTED, 20, self.workbook, RECORDED)
        self.assertEqual(first["measurements_appended"], 5)
        self.assertEqual(first["hr_bpm"], 61)
        self.assertEqual(first["hrv_rmssd_ms"], 20)
        self.assertEqual(first["rr_count"], 3)
        self.assertEqual(first["training_appended"], 0)
        self.assertEqual(first["errors"], 0)
        rows = [row for row in self.workbook.rows("measurements", date=day) if row.get("source") == "POLAR_H10"]
        self.assertEqual(sorted(row["metric"] for row in rows), ["heart_rate", "hrv_rmssd", "rr_interval", "rr_interval", "rr_interval"])
        self.assertTrue(all(row["context"] == "morning_resting" and row["device"] == "POLAR_H10" and row["updated_by"] == "polar-h10-sync" for row in rows))
        heart = next(row for row in rows if row["metric"] == "heart_rate")
        self.assertEqual(heart["timestamp"][:19], "2026-10-03T07:30:00")
        digest = sha256(self.path.read_bytes()).hexdigest()
        again = sync_polar("resting", self._samples(), STARTED, 20, self.workbook, RECORDED)
        self.assertEqual(again["measurements_exists"], 5)
        self.assertEqual(again["measurements_appended"], 0)
        self.assertEqual(sha256(self.path.read_bytes()).hexdigest(), digest)
        kept = self.workbook.rows("daily", date="2026-01-02")[0]
        self.assertEqual(kept["hrv_ms"], 41)
        self.assertEqual(kept["updated_by"], "fitbit-sync")

    def test_other_actor_measurement_conflicts(self):
        reading = resting_measurements(self._samples(), STARTED)[0]
        self.workbook.post("measurements", {**reading, "value": 99, "updated_by": "coach", "recorded_at": RECORDED})
        result = sync_polar("resting", self._samples(), STARTED, 20, self.workbook, RECORDED)
        self.assertGreater(result["measurements_conflicts"], 0)
        stored = [
            row for row in self.workbook.rows("measurements", date=STARTED.date().isoformat())
            if row.get("external_id") == reading["external_id"]
        ]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["value"], 99)

    def test_workout_stays_separate_from_fitbit_and_replays(self):
        record = workout_record(self._samples(), STARTED, 120)
        self.assertEqual(record["source"], "POLAR_H10")
        self.assertIn("device=POLAR_H10", record["details"])
        self.assertEqual(record["burn_role"], "session_hr_burn")
        self.assertTrue(record["session_id"].startswith("polar-h10:"))
        before = {
            (row["source"], row["session_id"], row.get("avg_hr"))
            for row in self.workbook.rows("training", date="2026-01-02")
        }
        first = sync_polar("workout", self._samples(), STARTED, 120, self.workbook, RECORDED)
        self.assertEqual(first["training_appended"], 1)
        self.assertEqual(first["measurements_appended"], 0)
        stored = [
            row for row in self.workbook.rows("training", session_id=record["session_id"])
            if row.get("source") == "POLAR_H10"
        ]
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["avg_hr"], 61)
        self.assertEqual(stored[0]["max_hr"], 62)
        after = {
            (row["source"], row["session_id"], row.get("avg_hr"))
            for row in self.workbook.rows("training", date="2026-01-02")
        }
        self.assertEqual(before, after)
        fitbit = [row for row in self.workbook.rows("training", session_id="S1") if row.get("source") == "FITBIT"]
        treadmill = [row for row in self.workbook.rows("training", session_id="S2") if row.get("source") == "TREADMILL"]
        self.assertEqual(len(fitbit), 1)
        self.assertEqual(len(treadmill), 1)
        digest = sha256(self.path.read_bytes()).hexdigest()
        again = sync_polar("workout", self._samples(), STARTED, 120, self.workbook, RECORDED)
        self.assertEqual(again["training_exists"], 1)
        self.assertEqual(again["training_appended"], 0)
        self.assertEqual(sha256(self.path.read_bytes()).hexdigest(), digest)

    def test_absent_capture_writes_nothing(self):
        digest = sha256(self.path.read_bytes()).hexdigest()
        result = sync_polar("resting", None, None, 0, self.workbook, RECORDED)
        self.assertFalse(result["found"])
        self.assertEqual(result["measurements_appended"], 0)
        self.assertEqual(result["errors"], 0)
        self.assertEqual(sha256(self.path.read_bytes()).hexdigest(), digest)

    def test_accidental_test_session_is_kept_out_of_workout_reporting(self):
        real = workout_record(self._samples(), STARTED, 903.44)
        accidental = dict(real)
        accidental["session_id"] = "polar-h10:2026-10-04T12:04:03-07:00"
        accidental["avg_hr"] = 79.01
        accidental["max_hr"] = 90
        accidental["details"] = mark_test_capture(accidental["details"])
        accidental["burn_role"] = "do_not_sum"
        fitbit = {
            "source": "FITBIT",
            "type": "TREADMILL",
            "burn_role": "do_not_sum",
            "details": "Treadmill run",
            "session_id": "fitbit:treadmill",
        }
        partial = dict(real)
        partial["session_id"] = "polar-h10:2026-10-04T12:21:32-07:00"
        partial["avg_hr"] = 127.22
        partial["max_hr"] = 151
        partial["duration_min"] = 15.06
        partial["details"] = mark_partial_capture(
            partial["details"],
            "Polar Flow reports duration 2:18:44, avg HR 136, max HR 162, 1569 kcal. Mismatch left in place.",
        )
        partial["burn_role"] = "partial_session"
        self.assertIn("tag=TEST/PRE-WORKOUT", accidental["details"])
        self.assertIn("context=accidental_pre_workout_test", accidental["details"])
        self.assertIn("Not the workout session", accidental["details"])
        self.assertIn("tag=REAL WORKOUT/PARTIAL CAPTURE DUE TO OLD 15-MIN LIMIT", partial["details"])
        self.assertIn("1569 kcal", partial["details"])
        self.assertEqual(partial["avg_hr"], 127.22)
        self.assertEqual(partial["max_hr"], 151)
        self.assertEqual(real["burn_role"], "session_hr_burn")
        self.assertNotIn("accidental_pre_workout_test", real["details"])
        self.assertNotIn("partial_15min_cap", real["details"])
        reportable = [row for row in (accidental, partial, real, fitbit) if reportable_polar_workout(row)]
        self.assertEqual(reportable, [real])
        self.assertEqual(seconds_limit("workout"), 3 * 60 * 60)
        self.assertEqual(seconds_limit("resting"), 900.0)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            morning = main(["resting", "--seconds", "10800"])
            workout = main(["workout", "--seconds", "10801"])
        self.assertEqual(morning, 2)
        self.assertEqual(workout, 2)
        self.assertEqual(buffer.getvalue().count('"error": "seconds"'), 2)

    def test_stop_summarizes_the_whole_capture_not_the_last_segment(self):
        early = [(100, [800.0]), (110, [810.0])]
        late = [(150, [820.0]), (160, [830.0])]
        ended = STARTED + timedelta(minutes=20)
        record = workout_record(early + late, STARTED, 20 * 60, ended=ended)
        self.assertEqual(record["avg_hr"], 130)
        self.assertEqual(record["max_hr"], 160)
        self.assertEqual(record["start_local"], STARTED.astimezone(zone()).isoformat(timespec="seconds"))
        self.assertIn("samples=4", record["details"])
        self.assertIn("end_local=" + ended.astimezone(zone()).isoformat(timespec="seconds"), record["details"])
        self.assertEqual(record["duration_min"], 20)
        self.assertEqual(record["burn_role"], "session_hr_burn")
        before = len(self.workbook.rows("measurements", date=STARTED.date().isoformat()))
        result = sync_polar("workout", early + late, STARTED, 20 * 60, self.workbook, RECORDED, ended=ended)
        self.assertEqual(result["training_appended"], 1)
        self.assertEqual(result["measurements_appended"], 0)
        self.assertEqual(result["hr_bpm"], 130)
        stored = self.workbook.rows("training", session_id=record["session_id"])
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["avg_hr"], 130)
        self.assertEqual(stored[0]["max_hr"], 160)
        self.assertEqual(len(self.workbook.rows("measurements", date=STARTED.date().isoformat())), before)

    def test_notify_uuid_is_the_heart_rate_measurement(self):
        seen = {}

        class Client(_NotifyClient):
            async def start_notify(self, uuid, callback):
                seen["uuid"] = uuid
                await super().start_notify(uuid, callback)

        async def find_device():
            return "strap"

        asyncio.run(collect_samples(0, find_device, lambda device: Client(device, [])))
        self.assertEqual(seen["uuid"], HR_MEASUREMENT_UUID)


if __name__ == "__main__":
    unittest.main()
