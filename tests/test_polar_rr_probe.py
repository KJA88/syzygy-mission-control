"""RR conversion for the Polar H10 probe. No radio and no stored readings."""
import importlib.util
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
