"""RR conversion for the Polar H10 probe. No radio and no stored readings."""
import asyncio
import contextlib
import importlib.util
import io
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _probe():
    spec = importlib.util.spec_from_file_location("polar_rr_probe", ROOT / "tools" / "polar_rr_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PolarRrProbeTests(unittest.TestCase):
    def test_raw_960_is_937_point_5_ms(self):
        probe = _probe()
        heart_rate, intervals = probe.parse_heart_rate_measurement(bytes([0x10, 63, 0xC0, 0x03]))
        self.assertEqual(heart_rate, 63)
        self.assertEqual(intervals, [937.5])
        self.assertEqual(probe.format_measurement(heart_rate, intervals), ["HR 63 bpm   RR 937.5 ms"])

    def test_every_rr_in_one_notification_is_reported(self):
        probe = _probe()
        # Two raw intervals: 1024 -> 1000.0 ms, 512 -> 500.0 ms.
        packet = bytes([0x10, 70, 0x00, 0x04, 0x00, 0x02])
        heart_rate, intervals = probe.parse_heart_rate_measurement(packet)
        self.assertEqual(heart_rate, 70)
        self.assertEqual(intervals, [1000.0, 500.0])
        self.assertEqual(
            probe.format_measurement(heart_rate, intervals),
            ["HR 70 bpm   RR 1000.0 ms", "HR 70 bpm   RR 500.0 ms"],
        )

    def test_uint16_heart_rate_and_energy_field_do_not_shift_rr(self):
        probe = _probe()
        # Flags: 16-bit HR and energy expended and RR. HR 300, energy ignored, raw RR 960.
        packet = bytes([0x19, 0x2C, 0x01, 0x34, 0x12, 0xC0, 0x03])
        heart_rate, intervals = probe.parse_heart_rate_measurement(packet)
        self.assertEqual(heart_rate, 300)
        self.assertEqual(intervals, [937.5])

    def test_missing_rr_flag_prints_heart_rate_only(self):
        probe = _probe()
        heart_rate, intervals = probe.parse_heart_rate_measurement(bytes([0x00, 63]))
        self.assertEqual((heart_rate, intervals), (63, []))
        self.assertEqual(probe.format_measurement(heart_rate, intervals), ["HR 63 bpm"])

    def test_truncated_rr_is_rejected(self):
        probe = _probe()
        with self.assertRaises(ValueError):
            probe.parse_heart_rate_measurement(bytes([0x10, 63, 0xC0]))

    def test_local_name_matches_when_device_name_is_missing(self):
        probe = _probe()
        polar = object()
        other = object()
        chosen, count = probe.select_h10([
            (other, "Phone", None, []),
            (polar, None, "Polar H10", []),
        ])
        self.assertIs(chosen, polar)
        self.assertEqual(count, 1)

    def test_heart_rate_service_matches_when_names_are_missing(self):
        probe = _probe()
        strap = object()
        chosen, count = probe.select_h10([
            (strap, None, None, ["0000180D-0000-1000-8000-00805F9B34FB"]),
        ])
        self.assertIs(chosen, strap)
        self.assertEqual(count, 1)
        self.assertIsNone(probe.advertisement_matches_h10("Other strap", None, []))

    def test_named_polar_wins_over_an_earlier_service_only_device(self):
        probe = _probe()
        other = object()
        polar = object()
        chosen, count = probe.select_h10([
            (other, None, None, [probe.HR_SERVICE_UUID]),
            (polar, None, "Polar H10", []),
        ])
        self.assertIs(chosen, polar)
        self.assertEqual(count, 1)

    def test_connect_timeout_retries_discovery_once_then_stops(self):
        probe = _probe()
        calls = {"find": 0, "devices": []}

        async def find_device():
            calls["find"] += 1
            device = object()
            calls["devices"].append(device)
            return device

        class Client:
            def __init__(self, device):
                calls["opened"] = calls.get("opened", []) + [device]

            async def __aenter__(self):
                raise asyncio.TimeoutError()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = asyncio.run(probe._listen(0, find_device, Client))
        text = stderr.getvalue()
        self.assertEqual(code, 1)
        self.assertEqual(calls["find"], 2)
        self.assertEqual(calls["opened"], calls["devices"])
        self.assertIn("Retrying discovery once.", text)
        self.assertIn("Connection to Polar H10 timed out.", text)
        self.assertNotIn("Traceback", text)

    def test_second_connection_attempt_can_succeed(self):
        probe = _probe()
        calls = {"find": 0}

        async def find_device():
            calls["find"] += 1
            return object()

        class Client:
            def __init__(self, device):
                self.device = device
                self.is_connected = True
                calls["opened"] = calls.get("opened", 0) + 1

            async def __aenter__(self):
                if calls["opened"] == 1:
                    raise asyncio.TimeoutError()
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def start_notify(self, uuid, callback):
                calls["uuid"] = uuid

            async def stop_notify(self, uuid):
                calls["stopped"] = uuid

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = asyncio.run(probe._listen(0, find_device, Client))
        self.assertEqual(code, 0)
        self.assertEqual(calls["find"], 2)
        self.assertEqual(calls["uuid"], probe.HR_MEASUREMENT_UUID)
        self.assertEqual(calls["stopped"], probe.HR_MEASUREMENT_UUID)
        self.assertIn("Retrying discovery once.", stderr.getvalue())
