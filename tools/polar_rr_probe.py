"""Print Polar H10 heart rate and RR intervals. Probe only.

Run with the system Python on the Pi, which already provides bleak:

    python3 tools/polar_rr_probe.py

Do not use the Mission Control virtualenv. This script does not store
readings, compute HRV, or call Mission Control.
"""

import argparse
import asyncio
import sys

HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
FLAG_HR_UINT16 = 0x01
FLAG_ENERGY_EXPENDED = 0x08
FLAG_RR_INTERVALS = 0x10


def parse_heart_rate_measurement(data):
    """Return heart rate in bpm and RR intervals in milliseconds.

    RR raw units are 1/1024 second, per the Heart Rate Measurement
    characteristic. A raw value of 960 is 937.5 ms.
    """
    if not data:
        raise ValueError("empty heart-rate notification")
    flags = data[0]
    index = 1
    if flags & FLAG_HR_UINT16:
        if len(data) < index + 2:
            raise ValueError("truncated heart-rate value")
        heart_rate = int.from_bytes(data[index:index + 2], "little")
        index += 2
    else:
        if len(data) < index + 1:
            raise ValueError("truncated heart-rate value")
        heart_rate = data[index]
        index += 1
    if flags & FLAG_ENERGY_EXPENDED:
        if len(data) < index + 2:
            raise ValueError("truncated energy field")
        index += 2
    intervals = []
    if flags & FLAG_RR_INTERVALS:
        remaining = data[index:]
        if len(remaining) % 2:
            raise ValueError("truncated RR interval")
        for offset in range(0, len(remaining), 2):
            raw = int.from_bytes(remaining[offset:offset + 2], "little")
            intervals.append(raw * 1000.0 / 1024.0)
    return heart_rate, intervals


def format_measurement(heart_rate, intervals):
    if not intervals:
        return ["HR %d bpm" % heart_rate]
    return ["HR %d bpm   RR %.1f ms" % (heart_rate, interval) for interval in intervals]


def _permission_denied(exc):
    text = str(exc).lower()
    markers = (
        "permission",
        "not authorized",
        "notauthorized",
        "access denied",
        "org.bluez.error.notauthorized",
        "org.freedesktop.dbus.error.accessdenied",
    )
    return any(marker in text for marker in markers)


async def _find_h10(scanner_cls):
    devices = await scanner_cls.discover(timeout=10.0)
    matches = [device for device in devices if (device.name or "").startswith("Polar H10")]
    if not matches:
        print(
            "No Polar H10 found. Wear the strap, moisten the electrode pads, "
            "and close other HR apps if the sensor is already connected.",
            file=sys.stderr,
        )
        return None
    if len(matches) > 1:
        print("Several Polar H10 advertisements. Using the first.", flush=True)
    print("Connecting to Polar H10.", flush=True)
    return matches[0]


async def run(seconds):
    try:
        from bleak import BleakClient, BleakScanner
        from bleak.exc import BleakError
    except ImportError:
        print(
            "bleak is not installed for this Python. Use system python3 on the Pi.",
            file=sys.stderr,
        )
        return 1

    try:
        device = await _find_h10(BleakScanner)
    except BleakError as exc:
        print("BLE error: %s" % exc, file=sys.stderr)
        if _permission_denied(exc):
            print(
                "Bluetooth access was denied for this user. "
                "No group, D-Bus, or Bluetooth configuration was changed.",
                file=sys.stderr,
            )
        return 1
    if device is None:
        return 1

    def on_measurement(_sender, data):
        try:
            heart_rate, intervals = parse_heart_rate_measurement(bytes(data))
        except ValueError as exc:
            print("Malformed heart-rate notification: %s" % exc, flush=True)
            return
        for line in format_measurement(heart_rate, intervals):
            print(line, flush=True)

    try:
        async with BleakClient(device) as client:
            await client.start_notify(HR_MEASUREMENT_UUID, on_measurement)
            print("Listening for %.0f seconds. Ctrl+C disconnects." % seconds, flush=True)
            try:
                await asyncio.sleep(seconds)
            finally:
                if client.is_connected:
                    await client.stop_notify(HR_MEASUREMENT_UUID)
    except BleakError as exc:
        print("BLE error: %s" % exc, file=sys.stderr)
        if _permission_denied(exc):
            print(
                "Bluetooth access was denied for this user. "
                "No group, D-Bus, or Bluetooth configuration was changed.",
                file=sys.stderr,
            )
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Print Polar H10 heart rate and RR intervals.")
    parser.add_argument("--seconds", type=float, default=25.0, help="listen duration (default 25)")
    args = parser.parse_args(argv)
    if args.seconds <= 0:
        print("seconds must be positive", file=sys.stderr)
        return 2
    try:
        return asyncio.run(run(args.seconds))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
