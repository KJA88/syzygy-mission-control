"""Print Polar H10 heart rate and RR intervals. Probe only.

Run with the system Python on the Pi, which already provides bleak:

    python3 tools/polar_rr_probe.py

Do not use the Mission Control virtualenv. This script does not store
readings, compute HRV, or call Mission Control.
"""

import argparse
import asyncio
import sys

HR_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
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


def _uuid_key(value):
    return str(value).replace("-", "").lower()


def advertisement_matches_h10(name, local_name, service_uuids):
    """Match a Polar H10 name, advertised local name, or Heart Rate Service."""
    for label in (name, local_name):
        if label and str(label).startswith("Polar H10"):
            return "name"
    wanted = _uuid_key(HR_SERVICE_UUID)
    for uuid in service_uuids or ():
        if _uuid_key(uuid) == wanted:
            return "service"
    return None


def select_h10(records):
    """Choose one device from (device, name, local_name, service_uuids) records.

    Named Polar H10 advertisements win over a Heart Rate Service match with no
    Polar name. Returns (device, candidate_count).
    """
    named = []
    by_service = []
    for device, name, local_name, service_uuids in records:
        kind = advertisement_matches_h10(name, local_name, service_uuids)
        if kind == "name":
            named.append(device)
        elif kind == "service":
            by_service.append(device)
    chosen = named or by_service
    if not chosen:
        return None, 0
    return chosen[0], len(chosen)


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
    found = await scanner_cls.discover(timeout=10.0, return_adv=True)
    records = []
    for device, advertisement in found.values():
        records.append((device, device.name, advertisement.local_name, advertisement.service_uuids))
    device, count = select_h10(records)
    if device is None:
        print(
            "No Polar H10 found. Wear the strap, moisten the electrode pads, "
            "and close other HR apps if the sensor is already connected.",
            file=sys.stderr,
        )
        return None
    if count > 1:
        print("Several Polar H10 advertisements. Using the first.", flush=True)
    print("Connecting to Polar H10.", flush=True)
    return device


async def _session(client_factory, device, seconds):
    def on_measurement(_sender, data):
        try:
            heart_rate, intervals = parse_heart_rate_measurement(bytes(data))
        except ValueError as exc:
            print("Malformed heart-rate notification: %s" % exc, flush=True)
            return
        for line in format_measurement(heart_rate, intervals):
            print(line, flush=True)

    async with client_factory(device) as client:
        await client.start_notify(HR_MEASUREMENT_UUID, on_measurement)
        print("Listening for %.0f seconds. Ctrl+C disconnects." % seconds, flush=True)
        try:
            await asyncio.sleep(seconds)
        finally:
            if client.is_connected:
                await client.stop_notify(HR_MEASUREMENT_UUID)


async def _listen(seconds, find_device, client_factory):
    device = await find_device()
    if device is None:
        return 1
    for attempt in (1, 2):
        try:
            await _session(client_factory, device, seconds)
            return 0
        except (asyncio.TimeoutError, TimeoutError):
            if attempt == 2:
                print("Connection to Polar H10 timed out.", file=sys.stderr)
                return 1
            print("Connection timed out. Retrying discovery once.", file=sys.stderr)
            device = await find_device()
            if device is None:
                return 1
    return 1


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

    def client_factory(device):
        return BleakClient(device)

    async def find_device():
        return await _find_h10(BleakScanner)

    try:
        return await _listen(seconds, find_device, client_factory)
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
