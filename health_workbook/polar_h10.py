"""Read a Polar H10 over Bluetooth LE and write through the workbook service.

Morning resting samples become Measurements. A workout capture becomes one
Training row. This module does not open the workbook file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import threading
from datetime import datetime, timedelta, timezone

from health_workbook.feeders import WorkbookClient, WorkbookClientError, _day, _equal, _num, zone

SOURCE = "POLAR_H10"
DEVICE = "POLAR_H10"
ACTOR = "polar-h10-sync"
CONTEXT = "morning_resting"
HR_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
CONNECT_TIMEOUT_S = 60.0
DISCOVER_TIMEOUT_S = 10.0
FLAG_HR_UINT16 = 0x01
FLAG_ENERGY_EXPENDED = 0x08
FLAG_RR_INTERVALS = 0x10
RR_MIN_MS = 300.0
RR_MAX_MS = 2000.0
MORNING_MAX_SECONDS = 900.0
WORKOUT_SAFETY_SECONDS = 3 * 60 * 60
TEST_CONTEXT = "accidental_pre_workout_test"
TEST_TAG = "TEST/PRE-WORKOUT"
TEST_NOTE = (
    "Accidental pre-workout test capture. Not the workout session. "
    "Exclude this row from workout reporting and session HR burn."
)
PARTIAL_CONTEXT = "partial_15min_cap"
PARTIAL_TAG = "REAL WORKOUT/PARTIAL CAPTURE DUE TO OLD 15-MIN LIMIT"
PARTIAL_NOTE = (
    "This row is only the opening capture stopped by the old 15-minute limit. "
    "It is not the complete workout. Do not replace these Pi values with another source."
)
_STOP = threading.Event()


def request_stop() -> None:
    """Ask an in-progress listen to finish and write what it has."""
    _STOP.set()


def _arm_stop() -> None:
    def _flag(_signum, _frame):
        request_stop()

    signal.signal(signal.SIGTERM, _flag)
    signal.signal(signal.SIGINT, _flag)


def parse_heart_rate_measurement(data):
    """Return heart rate in bpm and RR intervals in milliseconds.

    RR raw units are 1/1024 second. A raw value of 960 is 937.5 ms.
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
    for record in records:
        device, name, local_name, service_uuids = record[:4]
        kind = advertisement_matches_h10(name, local_name, service_uuids)
        if kind == "name":
            named.append(device)
        elif kind == "service":
            by_service.append(device)
    chosen = named or by_service
    if not chosen:
        return None, 0
    return chosen[0], len(chosen)


def describe_h10(records):
    """Name and address of the chosen H10. Other advertisements are omitted."""
    device, count = select_h10(records)
    if device is None:
        return {"found": False, "matches": 0}
    for item in records:
        if item[0] is device:
            name = item[1] or item[2] or getattr(device, "name", None)
            address = item[4] if len(item) > 4 else getattr(device, "address", None)
            return {"found": True, "name": name, "address": address, "matches": count}
    return {"found": False, "matches": 0}


def _accepted(interval):
    number = _num(interval)
    if number is None or number < RR_MIN_MS or number > RR_MAX_MS:
        return None
    return number


def hrv_from_intervals(intervals):
    """RMSSD in milliseconds. A rejected interval breaks the successive chain."""
    previous = None
    squares = []
    accepted = []
    rejected = 0
    for interval in intervals:
        value = _accepted(interval)
        if value is None:
            rejected += 1
            previous = None
            continue
        accepted.append(value)
        if previous is not None:
            delta = value - previous
            squares.append(delta * delta)
        previous = value
    rmssd = None
    if squares:
        rmssd = _num((sum(squares) / len(squares)) ** 0.5)
    mean_rr = _num(sum(accepted) / len(accepted)) if accepted else None
    return {"rmssd_ms": rmssd, "mean_rr_ms": mean_rr, "accepted": accepted, "rejected": rejected}


def _mean_hr(samples):
    values = []
    for heart_rate, _intervals in samples:
        number = _num(heart_rate)
        if number is None or number <= 0 or number > 250:
            continue
        values.append(number)
    if not values:
        return None, None
    return _num(sum(values) / len(values)), _num(max(values))


def _session_key(started: datetime) -> str:
    return started.astimezone(zone()).isoformat(timespec="seconds")


def _flatten(samples):
    ordered = []
    for _heart_rate, intervals in samples:
        if not intervals:
            continue
        ordered.extend(intervals)
    return ordered


def resting_measurements(samples, started: datetime) -> list[dict]:
    """Heart rate, RMSSD, and each accepted RR interval. Daily HRV is not written."""
    mean_hr, _max_hr = _mean_hr(samples)
    if mean_hr is None:
        return []
    stamp = _session_key(started)
    stats = hrv_from_intervals(_flatten(samples))
    rows = [_measurement(stamp, started, "heart_rate", mean_hr, "bpm", "heart_rate", "rr_accepted=%d" % len(stats["accepted"]))]
    if stats["rmssd_ms"] is not None:
        rows.append(_measurement(stamp, started, "hrv_rmssd", stats["rmssd_ms"], "ms", "hrv_rmssd", None))
    cursor = started
    index = 0
    for interval in _flatten(samples):
        cursor = cursor + timedelta(milliseconds=float(interval))
        value = _accepted(interval)
        if value is None:
            continue
        index += 1
        rows.append(_measurement(stamp, cursor, "rr_interval", value, "ms", "rr:%04d" % index, None))
    return rows


def seconds_limit(command: str) -> float:
    return WORKOUT_SAFETY_SECONDS if command == "workout" else MORNING_MAX_SECONDS


def _tagged(details: str, tag: str) -> bool:
    return ("tag=%s" % tag) in (details or "")


def mark_test_capture(details: str) -> str:
    """Keep the row, and mark it so reporting does not treat it as the workout."""
    text = (details or "").strip()
    if _tagged(text, TEST_TAG):
        return text
    note = "tag=%s. context=%s. %s" % (TEST_TAG, TEST_CONTEXT, TEST_NOTE)
    return (text + " " + note).strip()


def mark_partial_capture(details: str, comparison: str = "") -> str:
    """Keep the Pi summary, and mark it as an incomplete capture."""
    text = (details or "").strip()
    if _tagged(text, PARTIAL_TAG):
        return text
    note = "tag=%s. context=%s. %s" % (PARTIAL_TAG, PARTIAL_CONTEXT, PARTIAL_NOTE)
    if comparison:
        note = note + " " + comparison.strip()
    return (text + " " + note).strip()


def reportable_polar_workout(row: dict) -> bool:
    """A complete Polar workout is one session HR burn row, not a test, partial, or Fitbit row."""
    if not isinstance(row, dict):
        return False
    if row.get("source") != SOURCE or row.get("type") != "workout":
        return False
    if row.get("burn_role") != "session_hr_burn":
        return False
    details = str(row.get("details") or "")
    if TEST_CONTEXT in details or PARTIAL_CONTEXT in details:
        return False
    if _tagged(details, TEST_TAG) or _tagged(details, PARTIAL_TAG):
        return False
    return True


def workout_record(samples, started: datetime, duration_s: float, ended: datetime | None = None) -> dict | None:
    """One Training row for every heart-rate sample collected in the session."""
    mean_hr, max_hr = _mean_hr(samples)
    if mean_hr is None:
        return None
    local = started.astimezone(zone())
    if isinstance(ended, datetime):
        end_local = ended.astimezone(zone())
    else:
        end_local = local + timedelta(seconds=float(duration_s))
    minutes = _num(duration_s / 60.0)
    body = {
        "date": local.date().isoformat(),
        "session_id": "polar-h10:" + _session_key(started),
        "source": SOURCE,
        "type": "workout",
        "start_local": local.isoformat(timespec="seconds"),
        "duration_min": minutes,
        "avg_hr": mean_hr,
        "max_hr": max_hr,
        "burn_role": "session_hr_burn",
        "details": "device=%s samples=%d end_local=%s" % (
            DEVICE,
            len(samples),
            end_local.isoformat(timespec="seconds"),
        ),
        "updated_by": ACTOR,
    }
    return {key: value for key, value in body.items() if value is not None}


def _measurement(session, moment, metric, value, unit, suffix, notes) -> dict:
    body = {
        "timestamp": moment.astimezone(zone()).isoformat(timespec="seconds"),
        "metric": metric,
        "value": value,
        "unit": unit,
        "source": SOURCE,
        "device": DEVICE,
        "external_id": "polar-h10:" + CONTEXT + ":" + session + ":" + suffix,
        "context": CONTEXT,
        "updated_by": ACTOR,
    }
    if notes:
        body["notes"] = notes
    return body


def _empty(mode: str) -> dict:
    return {
        "feeder": "polar-h10",
        "mode": mode,
        "found": False,
        "hr_bpm": None,
        "hrv_rmssd_ms": None,
        "rr_count": 0,
        "rr_rejected": 0,
        "measurements_appended": 0,
        "measurements_exists": 0,
        "measurements_patched": 0,
        "measurements_conflicts": 0,
        "training_appended": 0,
        "training_exists": 0,
        "training_patched": 0,
        "training_conflicts": 0,
        "conflicts": [],
        "errors": 0,
        "reconnects": 0,
    }


def sync_polar(mode: str, samples, started: datetime | None, duration_s: float, workbook: WorkbookClient, recorded_at: str, ended: datetime | None = None) -> dict:
    result = _empty(mode)
    if samples is None or started is None:
        return result
    result["found"] = True
    mean_hr, _max_hr = _mean_hr(samples)
    stats = hrv_from_intervals(_flatten(samples))
    result["hr_bpm"] = mean_hr
    result["hrv_rmssd_ms"] = stats["rmssd_ms"]
    result["rr_count"] = len(stats["accepted"])
    result["rr_rejected"] = stats["rejected"]
    if mean_hr is None:
        return result
    if mode == "resting":
        for reading in resting_measurements(samples, started):
            _apply_measurement(workbook, reading, recorded_at, result)
        return result
    if mode == "workout":
        record = workout_record(samples, started, duration_s, ended=ended)
        if record is not None:
            _apply_training(workbook, record, recorded_at, result)
        return result
    result["errors"] += 1
    return result


def _apply_measurement(workbook: WorkbookClient, reading: dict, recorded_at: str, result: dict) -> None:
    day = _day(reading.get("timestamp"))
    try:
        rows = workbook.rows("measurements", date=day) if day else []
    except WorkbookClientError:
        result["errors"] += 1
        return
    existing = next((
        row for row in rows
        if row.get("source") == SOURCE and row.get("external_id") == reading["external_id"]
    ), None)
    payload = {**reading, "recorded_at": recorded_at}
    try:
        if existing is None:
            posted = workbook.post("measurements", payload)
            if posted.get("result") == "exists":
                result["measurements_exists"] += 1
            else:
                result["measurements_appended"] += 1
            return
        changes = {
            key: reading[key]
            for key in ("timestamp", "value", "unit", "device", "context")
            if key in reading and not _equal(existing.get(key), reading[key])
        }
        if not changes:
            result["measurements_exists"] += 1
            return
        if (existing.get("updated_by") or "") != ACTOR:
            result["measurements_conflicts"] += 1
            result["conflicts"].append(reading["metric"])
            return
        workbook.patch("measurements", existing["row_id"], changes, ACTOR, SOURCE, ACTOR, recorded_at)
        result["measurements_patched"] += 1
    except WorkbookClientError as error:
        if error.code == "conflict":
            result["measurements_conflicts"] += 1
            result["conflicts"].append(reading["metric"])
            return
        result["errors"] += 1


def _apply_training(workbook: WorkbookClient, record: dict, recorded_at: str, result: dict) -> None:
    try:
        rows = workbook.rows("training", session_id=record["session_id"])
    except WorkbookClientError:
        result["errors"] += 1
        return
    existing = next((
        row for row in rows
        if row.get("source") == SOURCE and row.get("session_id") == record["session_id"]
    ), None)
    payload = {**record, "recorded_at": recorded_at}
    try:
        if existing is None:
            posted = workbook.post("training", payload)
            if posted.get("result") == "exists":
                result["training_exists"] += 1
            else:
                result["training_appended"] += 1
            return
        changes = {
            key: record[key]
            for key in ("avg_hr", "max_hr", "duration_min", "start_local", "type", "details", "burn_role")
            if key in record and not _equal(existing.get(key), record[key])
        }
        if not changes:
            result["training_exists"] += 1
            return
        if (existing.get("updated_by") or "") != ACTOR:
            result["training_conflicts"] += 1
            result["conflicts"].append("training")
            return
        workbook.patch("training", existing["row_id"], changes, ACTOR, SOURCE, ACTOR, recorded_at)
        result["training_patched"] += 1
    except WorkbookClientError as error:
        if error.code == "conflict":
            result["training_conflicts"] += 1
            result["conflicts"].append("training")
            return
        result["errors"] += 1


def open_client(client_cls, device):
    """Construct a client from the discovered device with an explicit connect timeout."""
    return client_cls(device, timeout=CONNECT_TIMEOUT_S)


async def collect_samples(seconds, find_device, client_factory, sleep=None):
    """Listen until the deadline or request_stop. One disconnect rediscovers and continues."""
    _STOP.clear()
    pause = sleep or asyncio.sleep
    samples = []
    skipped = 0
    device = await find_device()
    if device is None:
        return {"found": False, "samples": None, "skipped": 0, "reconnects": 0, "elapsed_s": 0.0, "started_at": None, "ended_at": None}
    wall_started = datetime.now(zone())
    started = asyncio.get_running_loop().time()
    deadline = started + seconds
    reconnects = 0
    while not _STOP.is_set():
        try:
            skipped += await _listen_once(client_factory, device, samples, deadline, pause)
            break
        except (asyncio.TimeoutError, TimeoutError, ConnectionError):
            if reconnects >= 1 or asyncio.get_running_loop().time() >= deadline:
                break
            reconnects += 1
            device = await find_device()
            if device is None:
                break
    elapsed = max(0.0, asyncio.get_running_loop().time() - started)
    return {
        "found": True,
        "samples": samples,
        "skipped": skipped,
        "reconnects": reconnects,
        "elapsed_s": elapsed,
        "started_at": wall_started,
        "ended_at": datetime.now(zone()),
    }


async def _listen_once(client_factory, device, samples, deadline, pause):
    skipped = 0

    def on_measurement(_sender, data):
        nonlocal skipped
        try:
            heart_rate, intervals = parse_heart_rate_measurement(bytes(data))
        except (TypeError, ValueError):
            skipped += 1
            return
        samples.append((heart_rate, intervals))

    async with client_factory(device) as client:
        await client.start_notify(HR_MEASUREMENT_UUID, on_measurement)
        try:
            while asyncio.get_running_loop().time() < deadline and not _STOP.is_set():
                if getattr(client, "is_connected", True) is False:
                    raise ConnectionError("disconnected")
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                await pause(min(0.25, remaining))
        finally:
            if getattr(client, "is_connected", False):
                await client.stop_notify(HR_MEASUREMENT_UUID)
    return skipped


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


async def _scan_records(scanner_cls):
    found = await scanner_cls.discover(timeout=DISCOVER_TIMEOUT_S, return_adv=True)
    records = []
    for device, advertisement in found.values():
        records.append((device, device.name, getattr(advertisement, "local_name", None), getattr(advertisement, "service_uuids", None)))
    return records


async def discover_h10(scanner_cls):
    return describe_h10(await _scan_records(scanner_cls))


def _public(result: dict) -> dict:
    hidden = {"samples"}
    return {key: value for key, value in result.items() if key not in hidden}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Polar H10 collector for the health workbook.")
    parser.add_argument("command", choices=("discover", "resting", "workout"))
    parser.add_argument("--seconds", type=float, default=30.0)
    args = parser.parse_args(argv)
    if args.seconds <= 0 or args.seconds > seconds_limit(args.command):
        print(json.dumps({"feeder": "polar-h10", "error": "seconds", "errors": 1}))
        return 2
    try:
        from bleak import BleakClient, BleakScanner
        from bleak.exc import BleakError
    except ImportError:
        print(json.dumps({"feeder": "polar-h10", "error": "bleak_missing", "errors": 1}))
        return 1

    def client_factory(device):
        return open_client(BleakClient, device)

    async def find_device():
        records = await _scan_records(BleakScanner)
        device, _count = select_h10(records)
        return device

    try:
        if args.command == "discover":
            report = asyncio.run(discover_h10(BleakScanner))
            print(json.dumps({"feeder": "polar-h10", "mode": "discover", **report}, sort_keys=True))
            return 0 if report["found"] else 2
        token = os.environ.get("HEALTH_WORKBOOK_MAINTAIN_TOKEN", "")
        if not token:
            print(json.dumps({"feeder": "polar-h10", "auth": "missing", "errors": 1}))
            return 1
        started = datetime.now(zone())
        _arm_stop()
        captured = asyncio.run(collect_samples(args.seconds, find_device, client_factory))
    except BleakError as exc:
        code = "bluetooth_denied" if _permission_denied(exc) else "bluetooth_failed"
        print(json.dumps({"feeder": "polar-h10", "error": code, "errors": 1}))
        return 1
    except KeyboardInterrupt:
        return 0
    if not captured["found"]:
        print(json.dumps({"feeder": "polar-h10", "mode": args.command, "found": False, "errors": 0}, sort_keys=True))
        return 2
    recorded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    workbook = WorkbookClient(os.environ.get("HEALTH_WORKBOOK_URL", "http://127.0.0.1:5052"), token)
    try:
        listen_started = captured.get("started_at") or started
        result = sync_polar(
            args.command,
            captured["samples"],
            listen_started,
            captured["elapsed_s"],
            workbook,
            recorded_at,
            ended=captured.get("ended_at"),
        )
    except Exception:
        print(json.dumps({"feeder": "polar-h10", "mode": args.command, "error": "write_failed", "errors": 1}))
        return 1
    result["reconnects"] = captured["reconnects"]
    result["seconds"] = _num(captured["elapsed_s"])
    result["skipped"] = captured["skipped"]
    print(json.dumps(_public(result), sort_keys=True))
    if result["errors"]:
        return 1
    if result["hr_bpm"] is None:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
