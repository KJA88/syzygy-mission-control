"""Pull Withings Body Smart and BPM Connect readings through the workbook service."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from health_workbook.feeders import (
    WorkbookClient,
    WorkbookClientError,
    _blank,
    _day,
    _equal,
    _num,
    sync_dates,
    zone,
)

SOURCE = "WITHINGS"
ACTOR = "withings-sync"
BODY_SMART = "BODY_SMART"
BPM_CONNECT = "BPM_CONNECT"
TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
USER_URL = "https://wbsapi.withings.net/v2/user"
MEASURE_URL = "https://wbsapi.withings.net/measure"
ENV_FILE = "/home/KA_PI/syzygy-runtime/withings.env"
KG_TO_LB = 2.2046226218
MEASURE_TYPES = "1,5,6,8,9,10,11,76,77,88,170,226"
# Known model ids for this account. Any other id stays unmapped.
MODEL_IDS = {
    16: BODY_SMART,
    45: BPM_CONNECT,
}
MODEL_NAMES = {
    "body smart": BODY_SMART,
    "wbs13": BODY_SMART,
    "bpm connect": BPM_CONNECT,
}
# Mass types are kilograms. Ratio, index, pulse, and BMR stay in the API unit.
METRICS = {
    1: ("weight", "lb", True),
    5: ("fat_free_mass", "lb", True),
    6: ("body_fat_pct", "percent", False),
    8: ("fat_mass", "lb", True),
    9: ("diastolic", "mmHg", False),
    10: ("systolic", "mmHg", False),
    11: ("heart_rate", "bpm", False),
    76: ("muscle_mass", "lb", True),
    77: ("body_water", "lb", True),
    88: ("bone_mass", "lb", True),
    170: ("visceral_fat", "index", False),
    226: ("bmr", "kcal", False),
}
DAILY_FROM = {"weight": "weight_lb", "body_fat_pct": "body_fat_pct"}
COMPARE = ("timestamp", "metric", "value", "value2", "unit", "device")


class WithingsError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def device_kind(device: dict) -> str | None:
    model_id = device.get("model_id")
    if isinstance(model_id, int) and not isinstance(model_id, bool) and model_id in MODEL_IDS:
        return MODEL_IDS[model_id]
    name = device.get("model")
    if not isinstance(name, str):
        return None
    return MODEL_NAMES.get(" ".join(name.casefold().split()))


def _model_id(device: dict) -> int | None:
    model_id = device.get("model_id")
    if isinstance(model_id, bool) or not isinstance(model_id, int):
        return None
    return model_id


def decode_measure(measure: dict):
    if not isinstance(measure, dict):
        return None
    raw = measure.get("value")
    unit = measure.get("unit", 0)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if isinstance(unit, bool) or not isinstance(unit, int) or unit < -12 or unit > 6:
        return None
    number = float(raw) * (10 ** unit)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def kg_to_lb(kilograms: float):
    return _num(kilograms * KG_TO_LB)


def _stamp(epoch: int) -> tuple[str, str]:
    local = datetime.fromtimestamp(epoch, tz=zone())
    return local.isoformat(timespec="seconds"), local.date().isoformat()


def map_withings(devices: list, groups: list, dates: list[str]) -> tuple[list[dict], dict, int]:
    kinds = {}
    for device in devices or []:
        if not isinstance(device, dict):
            continue
        kind = device_kind(device)
        ident = device.get("deviceid")
        if kind and isinstance(ident, str) and ident:
            kinds[ident] = (kind, _model_id(device))
    wanted = set(dates)
    rows = []
    seen = set()
    latest = {day: {} for day in dates}
    skipped = 0
    for group in groups or []:
        if not isinstance(group, dict):
            skipped += 1
            continue
        record = kinds.get(group.get("deviceid"))
        kind, model_id = record if record else (None, None)
        grpid = group.get("grpid")
        epoch = group.get("date")
        if kind is None or grpid is None or isinstance(epoch, bool) or not isinstance(epoch, (int, float)):
            skipped += 1
            continue
        stamp, day = _stamp(int(epoch))
        if day not in wanted:
            skipped += 1
            continue
        decoded = {}
        for measure in group.get("measures") or []:
            if not isinstance(measure, dict):
                continue
            kind_type = measure.get("type")
            if kind_type not in METRICS:
                continue
            number = decode_measure(measure)
            if number is not None:
                decoded[kind_type] = number
        context = None if model_id is None else "model_id=" + str(model_id)
        if kind == BPM_CONNECT:
            systolic = decoded.get(10)
            diastolic = decoded.get(9)
            if systolic is not None and diastolic is not None:
                _add(rows, seen, _reading(grpid, stamp, "blood_pressure", _num(systolic), "mmHg", kind, _num(diastolic), context))
            pulse = decoded.get(11)
            if pulse is not None:
                _add(rows, seen, _reading(grpid, stamp, "heart_rate", _num(pulse), "bpm", kind, None, context))
            continue
        for type_id, (metric, unit, mass) in METRICS.items():
            if type_id in (9, 10) or type_id not in decoded:
                continue
            value = kg_to_lb(decoded[type_id]) if mass else _num(decoded[type_id])
            if value is None:
                continue
            _add(rows, seen, _reading(grpid, stamp, metric, value, unit, kind, None, context))
            if metric in DAILY_FROM:
                previous = latest[day].get(metric)
                if previous is None or int(epoch) >= previous[0]:
                    latest[day][metric] = (int(epoch), value)
    daily = {}
    for day, metrics in latest.items():
        fields = {DAILY_FROM[name]: value for name, (_epoch, value) in metrics.items()}
        if fields:
            daily[day] = fields
    return rows, daily, skipped


def _reading(grpid, stamp, metric, value, unit, device, value2, context) -> dict:
    body = {
        "timestamp": stamp,
        "metric": metric,
        "value": value,
        "unit": unit,
        "source": SOURCE,
        "device": device,
        "external_id": "withings:" + str(grpid) + ":" + metric,
        "updated_by": ACTOR,
    }
    if value2 is not None:
        body["value2"] = value2
    if context:
        body["context"] = context
    return body


def _add(rows: list, seen: set, reading: dict) -> None:
    if reading["external_id"] in seen:
        return
    seen.add(reading["external_id"])
    rows.append(reading)


def _daily_changes(existing: dict | None, incoming: dict) -> tuple[dict, list[str]]:
    changes = {}
    conflicts = []
    for key, value in incoming.items():
        current = None if existing is None else existing.get(key)
        if existing is not None and _equal(current, value):
            continue
        if existing is not None and not _blank(current) and (existing.get("updated_by") or "") != ACTOR:
            conflicts.append(key)
            continue
        changes[key] = value
    return changes, conflicts


def _measurement_diff(existing: dict, reading: dict) -> dict:
    changes = {}
    for key in COMPARE:
        if key not in reading:
            if key == "value2" and _blank(existing.get(key)):
                continue
            if key == "value2":
                continue
        if key not in reading:
            continue
        if not _equal(existing.get(key), reading[key]):
            changes[key] = reading[key]
    return changes


class WithingsClient:
    def __init__(self, client_id: str, client_secret: str, access_token: str, refresh_token: str, transport=None, env_file: str | None = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.transport = transport or urllib_transport
        self.env_file = env_file
        self.auth = "ok"

    def load_window(self, dates: list[str]) -> tuple[list, list]:
        if not self.client_id or not self.client_secret or not self.refresh_token:
            raise WithingsError("missing")
        if not self.access_token:
            self._refresh()
        start, end = _window(dates)
        devices = self._authorized(USER_URL, {"action": "getdevice"}).get("devices") or []
        groups = []
        offset = None
        for _ in range(10):
            fields = {
                "action": "getmeas",
                "category": 1,
                "startdate": start,
                "enddate": end,
                "meastypes": MEASURE_TYPES,
            }
            if offset is not None:
                fields["offset"] = offset
            page = self._authorized(MEASURE_URL, fields)
            batch = page.get("measuregrps") or []
            if isinstance(batch, list):
                groups.extend(batch)
            if page.get("more") != 1 or page.get("offset") in (None, offset):
                break
            offset = page.get("offset")
        return devices if isinstance(devices, list) else [], groups

    def _authorized(self, url: str, fields: dict) -> dict:
        status, body = self._send(url, fields, self.access_token)
        if _unauthorized(status, body):
            self._refresh()
            status, body = self._send(url, fields, self.access_token)
            if _unauthorized(status, body):
                raise WithingsError("refresh_failed")
        return _ok_body(status, body)

    def _refresh(self) -> None:
        status, body = self._send(TOKEN_URL, {
            "action": "requesttoken",
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
        }, None)
        payload = _ok_body(status, body)
        access = payload.get("access_token")
        refresh = payload.get("refresh_token")
        if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
            raise WithingsError("refresh_failed")
        self.access_token = access
        self.refresh_token = refresh
        self.auth = "refreshed"
        if self.env_file:
            persist_tokens(Path(self.env_file), access, refresh)

    def _send(self, url: str, fields: dict, bearer: str | None):
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if bearer:
            headers["Authorization"] = "Bearer " + bearer
        try:
            return self.transport(url, urlencode(fields).encode("utf-8"), headers)
        except Exception as error:
            raise WithingsError("api_failed") from error


def _unauthorized(status: int, body) -> bool:
    if status == 401:
        return True
    return isinstance(body, dict) and body.get("status") == 401


def _ok_body(status: int, body) -> dict:
    if status >= 300 or not isinstance(body, dict) or body.get("status") not in (0, None):
        raise WithingsError("api_failed")
    payload = body.get("body")
    return payload if isinstance(payload, dict) else {}


def urllib_transport(url: str, data: bytes, headers: dict):
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        try:
            parsed = json.loads(error.read().decode())
        except (OSError, UnicodeError, json.JSONDecodeError):
            parsed = {}
        finally:
            error.close()
        return error.code, parsed


def persist_tokens(path: Path, access_token: str, refresh_token: str) -> None:
    if path.is_symlink():
        raise WithingsError("token_store")
    lines = []
    seen_access = False
    seen_refresh = False
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("WITHINGS_ACCESS_TOKEN="):
                lines.append("WITHINGS_ACCESS_TOKEN=" + access_token)
                seen_access = True
            elif line.startswith("WITHINGS_REFRESH_TOKEN="):
                lines.append("WITHINGS_REFRESH_TOKEN=" + refresh_token)
                seen_refresh = True
            else:
                lines.append(line)
    if not seen_access:
        lines.append("WITHINGS_ACCESS_TOKEN=" + access_token)
    if not seen_refresh:
        lines.append("WITHINGS_REFRESH_TOKEN=" + refresh_token)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _window(dates: list[str]) -> tuple[int, int]:
    start = datetime.fromisoformat(dates[0]).replace(tzinfo=zone())
    end = datetime.fromisoformat(dates[-1]).replace(tzinfo=zone()) + timedelta(days=1) - timedelta(seconds=1)
    return int(start.timestamp()), int(end.timestamp())


def sync_withings(load, workbook: WorkbookClient, dates: list[str], recorded_at: str) -> dict:
    result = {
        "feeder": "withings",
        "auth": "ok",
        "measurements_appended": 0,
        "measurements_patched": 0,
        "measurements_exists": 0,
        "measurements_conflicts": 0,
        "measurements_written": 0,
        "metrics_written": [],
        "daily_appended": 0,
        "daily_patched": 0,
        "daily_exists": 0,
        "daily_conflicts": 0,
        "fields_written": [],
        "conflicts": [],
        "skipped_groups": 0,
        "errors": 0,
    }
    try:
        loaded = load()
    except WithingsError as error:
        result["auth"] = error.code
        result["errors"] = 1
        return result
    devices, groups = loaded[0], loaded[1]
    if len(loaded) > 2 and isinstance(loaded[2], str):
        result["auth"] = loaded[2]
    readings, daily, skipped = map_withings(devices, groups, dates)
    result["skipped_groups"] = skipped
    result["recognized"] = _recognized(devices)
    result["scale_groups"] = _group_count(readings, BODY_SMART)
    for reading in readings:
        _apply_measurement(workbook, reading, recorded_at, result)
    for day in dates:
        _apply_withings_daily(workbook, day, daily.get(day) or {}, recorded_at, result)
    result["measurements_written"] = result["measurements_appended"] + result["measurements_patched"]
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
            workbook.post("measurements", payload)
            result["measurements_appended"] += 1
            _remember(result, reading["metric"])
            return
        changes = _measurement_diff(existing, reading)
        if not changes:
            result["measurements_exists"] += 1
            return
        if (existing.get("updated_by") or "") != ACTOR:
            result["measurements_conflicts"] += 1
            result["conflicts"].append("measurement")
            return
        workbook.patch("measurements", existing["row_id"], changes, ACTOR, SOURCE, ACTOR, recorded_at)
        result["measurements_patched"] += 1
        _remember(result, reading["metric"])
    except WorkbookClientError as error:
        if error.code == "conflict":
            result["measurements_conflicts"] += 1
            result["conflicts"].append("measurement")
            return
        result["errors"] += 1


def _apply_withings_daily(workbook: WorkbookClient, day: str, incoming: dict, recorded_at: str, result: dict) -> None:
    if not incoming:
        return
    try:
        matched = [row for row in workbook.rows("daily", date=day) if _day(row.get("date")) == day]
        if len(matched) > 1:
            result["errors"] += 1
            return
        existing = matched[0] if matched else None
        changes, conflicts = _daily_changes(existing, incoming)
    except WorkbookClientError:
        result["errors"] += 1
        return
    result["daily_conflicts"] += len(conflicts)
    result["conflicts"].extend(day + ":" + name for name in conflicts)
    if not changes:
        result["daily_exists"] += 1
        return
    try:
        if existing is None:
            workbook.post("daily", {
                "date": day,
                "source": SOURCE,
                "updated_by": ACTOR,
                "recorded_at": recorded_at,
                **changes,
            })
            result["daily_appended"] += 1
        else:
            workbook.patch("daily", existing["row_id"], changes, ACTOR, SOURCE, ACTOR, recorded_at)
            result["daily_patched"] += 1
        result["fields_written"] = sorted(set(result["fields_written"]) | set(changes))
    except WorkbookClientError as error:
        if error.code == "conflict":
            result["daily_conflicts"] += 1
            result["conflicts"].append(day + ":row")
            return
        result["errors"] += 1


def _recognized(devices: list) -> list[dict]:
    found = []
    seen = set()
    for device in devices or []:
        if not isinstance(device, dict):
            continue
        kind = device_kind(device)
        model_id = _model_id(device)
        if kind is None or model_id is None or (model_id, kind) in seen:
            continue
        seen.add((model_id, kind))
        found.append({"model_id": model_id, "device": kind})
    return sorted(found, key=lambda item: item["model_id"])


def _group_count(readings: list[dict], device: str) -> int:
    groups = set()
    for reading in readings:
        if reading.get("device") != device:
            continue
        parts = str(reading.get("external_id") or "").split(":")
        if len(parts) >= 3:
            groups.add(parts[1])
    return len(groups)


def _remember(result: dict, metric: str) -> None:
    if metric not in result["metrics_written"]:
        result["metrics_written"] = sorted([*result["metrics_written"], metric])


def main() -> int:
    token = os.environ.get("HEALTH_WORKBOOK_MAINTAIN_TOKEN", "")
    if not token:
        print(json.dumps({"feeder": "withings", "auth": "missing", "errors": 1}))
        return 1
    client = WithingsClient(
        os.environ.get("WITHINGS_CLIENT_ID", ""),
        os.environ.get("WITHINGS_CLIENT_SECRET", ""),
        os.environ.get("WITHINGS_ACCESS_TOKEN", ""),
        os.environ.get("WITHINGS_REFRESH_TOKEN", ""),
        env_file=os.environ.get("WITHINGS_ENV_FILE", ENV_FILE),
    )
    dates = sync_dates()
    recorded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    workbook = WorkbookClient(os.environ.get("HEALTH_WORKBOOK_URL", "http://127.0.0.1:5052"), token)

    def load():
        devices, groups = client.load_window(dates)
        return devices, groups, client.auth

    try:
        result = sync_withings(load, workbook, dates, recorded_at)
    except Exception:
        print(json.dumps({"feeder": "withings", "auth": "api_failed", "errors": 1}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
