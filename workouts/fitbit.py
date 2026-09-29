"""Normalize Fitbit exercise records already parsed by the local Fitbit MCP.

The parser in the Fitbit service returns display name, exercise type, start,
end, duration minutes, calories, steps, distance miles, average heart rate,
numeric active-zone minutes, light/moderate/vigorous/peak zone minutes, pace,
GPS, device display name, platform, and recording method. It does not return a
workout id or max heart rate. Daily active-zone minutes and daily time-in-zone
are separate rollups. This module calls only those four local tools.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SOURCE = "fitbit"
TIMEZONE = "America/Los_Angeles"
DEFAULT_URL = "http://127.0.0.1:8010/mcp"
WINDOW_DAYS = 7
CACHE_TTL_S = 10 * 60
ALLOWED_TOOLS = frozenset({
    "get_fitbit_exercises",
    "get_fitbit_exercise_history",
    "get_fitbit_active_zone_minutes_history",
    "get_fitbit_time_in_heart_rate_zone_history",
})
_ZONE_KEYS = ("light_minutes", "moderate_minutes", "vigorous_minutes", "peak_minutes")
_AZM_KEYS = (
    "active_zone_minutes",
    "fat_burn_zone_minutes",
    "cardio_zone_minutes",
    "peak_zone_minutes",
)


class FitbitUnavailable(Exception):
    """The local Fitbit MCP could not return a workout summary."""


def _zone():
    try:
        return ZoneInfo(TIMEZONE)
    except ZoneInfoNotFoundError:
        # Windows test hosts may lack the IANA database. January synthetic
        # dates use this same Pacific standard offset. The Pi has zoneinfo.
        return timezone(timedelta(hours=-8))


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        rounded = round(value, 2)
        if rounded == int(rounded):
            return int(rounded)
        return rounded
    return value


def _text(value):
    if isinstance(value, str) and value:
        return value
    return None


def _sum(values):
    numbers = [value for value in values if value is not None]
    if not numbers:
        return None
    total = sum(numbers)
    return _num(total)


def _source_key(record):
    parts = [
        record.get("type"),
        record.get("start"),
        record.get("end"),
        record.get("duration_minutes"),
        record.get("distance_miles"),
        record.get("calories"),
    ]
    return "fitbit:" + "|".join("" if part is None else str(part) for part in parts)


def normalize_exercise(record):
    """Copy parser fields. Unknown keys, including max heart rate, are dropped."""
    if not isinstance(record, dict):
        return None
    zones_in = record.get("heart_rate_zones")
    zones_in = zones_in if isinstance(zones_in, dict) else {}
    name = _text(record.get("exercise"))
    exercise_type = _text(record.get("type"))
    start = _text(record.get("start"))
    if name is None and exercise_type is None and start is None:
        return None
    return {
        "source": SOURCE,
        "source_key": _source_key(record),
        "name": name,
        "exercise_type": exercise_type,
        "start": start,
        "end": _text(record.get("end")),
        "duration_minutes": _num(record.get("duration_minutes")),
        "calories": _num(record.get("calories")),
        "steps": _num(record.get("steps")),
        "distance_miles": _num(record.get("distance_miles")),
        "average_heart_rate_bpm": _num(record.get("average_heart_rate_bpm")),
        "active_zone_minutes": _num(record.get("active_zone_minutes")),
        "heart_rate_zones": {key: _num(zones_in.get(key)) for key in _ZONE_KEYS},
        "average_pace_seconds_per_meter": _num(record.get("average_pace_seconds_per_meter")),
        "average_pace_minutes_per_mile": _num(record.get("average_pace_minutes_per_mile")),
        "has_gps": record.get("has_gps") if isinstance(record.get("has_gps"), bool) else None,
        "device": _text(record.get("device")),
        "platform": _text(record.get("platform")),
        "recording_method": _text(record.get("recording_method")),
    }


def dedupe_exercises(records):
    workouts = []
    seen = set()
    for record in records:
        workout = normalize_exercise(record)
        if workout is None or workout["source_key"] in seen:
            continue
        seen.add(workout["source_key"])
        workouts.append(workout)
    workouts.sort(key=lambda item: item["start"] or "", reverse=True)
    return workouts


def _as_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value]
    return []


def history_rows(value):
    """Daily history tools return {start_date, end_date, records:[...]}.

    A bare list is accepted for tests. The wrapper itself is not a day row.
    """
    if isinstance(value, dict) and "records" in value:
        rows = value.get("records")
        return rows if isinstance(rows, list) else []
    return _as_list(value)


def _civil_date(start, zone):
    if not isinstance(start, str):
        return None
    try:
        parsed = datetime.fromisoformat(start.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone).date()


def _workout_totals(workouts):
    zones = {
        key: _sum(workout["heart_rate_zones"].get(key) for workout in workouts)
        for key in _ZONE_KEYS
    }
    return {
        "duration_minutes": _sum(workout.get("duration_minutes") for workout in workouts),
        "calories": _sum(workout.get("calories") for workout in workouts),
        "distance_miles": _sum(workout.get("distance_miles") for workout in workouts),
        "steps": _sum(workout.get("steps") for workout in workouts),
        "active_zone_minutes": _sum(workout.get("active_zone_minutes") for workout in workouts),
        "heart_rate_zones": zones,
    }


def normalize_daily_active_zone_minutes(records):
    days = []
    for record in history_rows(records):
        if not isinstance(record, dict):
            continue
        day = {"date": _text(record.get("date"))}
        day.update({key: _num(record.get(key)) for key in _AZM_KEYS})
        if day["date"] is None and all(day[key] is None for key in _AZM_KEYS):
            continue
        days.append(day)
    return {
        "available": True,
        "days": days,
        "totals": {key: _sum(day.get(key) for day in days) for key in _AZM_KEYS},
    }


def normalize_daily_time_in_zone(records):
    totals = {}
    for record in history_rows(records):
        if not isinstance(record, dict):
            continue
        for zone in record.get("zones") or []:
            if not isinstance(zone, dict):
                continue
            name = _text(zone.get("zone"))
            seconds = _num(zone.get("duration_seconds"))
            if name is None or seconds is None:
                continue
            totals[name] = _num((totals.get(name) or 0) + seconds)
    return {"available": True, "duration_seconds": totals}


def _decode(body):
    if not body:
        return None
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    if text.startswith("{"):
        return json.loads(text)
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload and payload != "[DONE]":
                return json.loads(payload)
    raise FitbitUnavailable()


def unwrap_tool_result(message):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "error" in message:
        raise FitbitUnavailable()
    result = message.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        raise FitbitUnavailable()
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and set(structured) == {"result"}:
        return structured["result"]
    if isinstance(structured, (dict, list)):
        return structured
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                return json.loads(item["text"])
    raise FitbitUnavailable()


def _loopback_mcp_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
        raise FitbitUnavailable()
    if parsed.port != 8010 or parsed.path != "/mcp":
        raise FitbitUnavailable()
    return url


def urllib_transport(url, payload, headers, timeout_s):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code, {}, b""


class FitbitMcp:
    """Stateless JSON-RPC client for the loopback Fitbit MCP only."""

    def __init__(self, url=DEFAULT_URL, transport=urllib_transport, timeout_s=8.0):
        self.url = _loopback_mcp_url(url)
        self.transport = transport
        self.timeout_s = timeout_s

    def call_tool(self, name, arguments):
        if name not in ALLOWED_TOOLS or not isinstance(arguments, dict):
            raise FitbitUnavailable()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        status, response_headers, body = self.transport(self.url, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "syzygy-mission-control", "version": "0.1.0"},
            },
        }, headers, self.timeout_s)
        message = _decode(body)
        if status >= 300 or not isinstance(message, dict):
            raise FitbitUnavailable()
        result = message.get("result")
        if not isinstance(result, dict) or result.get("protocolVersion") != "2024-11-05":
            raise FitbitUnavailable()
        session = None
        for key, value in response_headers.items():
            if key.lower() == "mcp-session-id" and value:
                session = value
        if session:
            headers["Mcp-Session-Id"] = session
        headers["MCP-Protocol-Version"] = "2024-11-05"
        self.transport(self.url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, headers, self.timeout_s)
        status, _, body = self.transport(self.url, {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }, headers, self.timeout_s)
        if status >= 300:
            raise FitbitUnavailable()
        return unwrap_tool_result(_decode(body))


class WorkoutSource:
    def __init__(self, call_tool, now=None, clock=None):
        self.call_tool = call_tool
        self.now = now or (lambda: datetime.now(_zone()))
        self._clock = clock or time.monotonic
        self._cache = {}

    def _cached(self, key):
        item = self._cache.get(key)
        if item is None:
            return None
        expires, payload = item
        if self._clock() >= expires:
            return None
        return payload

    def _store(self, key, payload):
        self._cache[key] = (self._clock() + CACHE_TTL_S, payload)
        return payload

    def _zone_now(self):
        current = self.now()
        zone = _zone()
        if current.tzinfo is None:
            return current.replace(tzinfo=zone)
        return current.astimezone(zone)

    def window(self):
        end = self._zone_now().date()
        start = end - timedelta(days=WINDOW_DAYS - 1)
        return start, end

    def _offline(self):
        return {"available": False, "reason": "FITBIT_UNAVAILABLE", "source": SOURCE, "workouts": []}

    def recent(self, limit):
        key = ("recent", limit)
        cached = self._cached(key)
        if cached is not None:
            return cached
        try:
            records = self.call_tool("get_fitbit_exercises", {"limit": limit})
        except Exception:
            return self._store(key, self._offline())
        payload = {"available": True, "source": SOURCE, "workouts": dedupe_exercises(_as_list(records))[:limit]}
        return self._store(key, payload)

    def summary(self):
        cached = self._cached("summary")
        if cached is not None:
            return cached
        return self._store("summary", self._load_summary())

    def _load_summary(self):
        start, end = self.window()
        try:
            records = self.call_tool("get_fitbit_exercise_history", {
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            })
        except Exception:
            return self._offline()
        zone = _zone()
        workouts = []
        for workout in dedupe_exercises(_as_list(records)):
            civil = _civil_date(workout.get("start"), zone)
            if civil is not None and start <= civil <= end:
                workouts.append(workout)
        payload = {
            "available": True,
            "source": SOURCE,
            "timezone": TIMEZONE,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "workout_count": len(workouts),
            "workouts": workouts,
            "totals": _workout_totals(workouts),
            "daily_active_zone_minutes": self._daily(
                "get_fitbit_active_zone_minutes_history",
                start,
                end,
                normalize_daily_active_zone_minutes,
            ),
            "daily_time_in_heart_rate_zone": self._daily(
                "get_fitbit_time_in_heart_rate_zone_history",
                start,
                end,
                normalize_daily_time_in_zone,
            ),
        }
        return payload

    def _daily(self, tool, start, end, normalize):
        try:
            records = self.call_tool(tool, {
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            })
        except Exception:
            return {"available": False}
        return normalize(records)


def open_fitbit(url=None):
    return WorkoutSource(FitbitMcp(url or os.environ.get("FITBIT_MCP_URL", DEFAULT_URL)).call_tool)
