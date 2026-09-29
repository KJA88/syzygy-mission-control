"""Normalize Fitbit health records already parsed by the local Fitbit MCP.

Daily history tools that use ``_get_history`` return
``{start_date, end_date, records:[...]}``. Sleep, resting heart rate, HRV,
SpO2, respiratory rate, sleep temperature, heart-rate samples, zone bounds,
and weight histories return lists. Dates from ``parse_date`` are
``{year, month, day}``. This module does not invent a readiness score, a
workout id, or Polar HRV.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

from workouts.fitbit import (
    CACHE_TTL_S,
    DEFAULT_URL,
    SOURCE,
    TIMEZONE,
    FitbitMcp,
    _num,
    _text,
    _zone,
    history_rows,
)

HEALTH_TOOLS = frozenset({
    "get_fitbit_sleep",
    "get_fitbit_resting_heart_rate",
    "get_fitbit_hrv",
    "get_fitbit_oxygen_saturation",
    "get_fitbit_respiratory_rate",
    "get_fitbit_sleep_temperature",
    "get_fitbit_heart_rate",
    "get_fitbit_heart_rate_zones",
    "get_fitbit_weight",
    "get_fitbit_steps",
    "get_fitbit_distance",
    "get_fitbit_active_zone_minutes",
    "get_fitbit_total_calories",
    "get_fitbit_active_energy_burned",
    "get_fitbit_floors",
    "get_fitbit_active_minutes",
    "get_fitbit_time_in_heart_rate_zone",
    "get_fitbit_sleep_history",
    "get_fitbit_resting_heart_rate_history",
    "get_fitbit_hrv_history",
    "get_fitbit_oxygen_saturation_history",
    "get_fitbit_respiratory_rate_history",
    "get_fitbit_sleep_temperature_history",
    "get_fitbit_weight_history",
    "get_fitbit_steps_history",
    "get_fitbit_distance_history",
    "get_fitbit_active_zone_minutes_history",
    "get_fitbit_total_calories_history",
    "get_fitbit_active_energy_burned_history",
    "get_fitbit_floors_history",
    "get_fitbit_active_minutes_history",
    "get_fitbit_time_in_heart_rate_zone_history",
})

SUMMARY_DAYS = 7
TREND_DAYS = 30
ADDITIVE = frozenset({
    "steps",
    "distance_miles",
    "active_zone_minutes",
    "active_minutes_total",
    "total_calories_kcal",
    "active_energy_kcal",
    "floors",
    "minutes_asleep",
})
SERIES = (
    ("minutes_asleep", "get_fitbit_sleep_history", "min", "local_wake_date"),
    ("resting_heart_rate_bpm", "get_fitbit_resting_heart_rate_history", "bpm", "date"),
    ("average_hrv_ms", "get_fitbit_hrv_history", "ms", "date"),
    ("average_spo2_percent", "get_fitbit_oxygen_saturation_history", "percent", "date"),
    ("breaths_per_minute", "get_fitbit_respiratory_rate_history", "breaths_per_minute", "date"),
    ("difference_from_baseline_celsius", "get_fitbit_sleep_temperature_history", "celsius", "date"),
    ("weight_pounds", "get_fitbit_weight_history", "lb", "date"),
    ("steps", "get_fitbit_steps_history", "count", "date"),
    ("distance_miles", "get_fitbit_distance_history", "mi", "date"),
    ("active_zone_minutes", "get_fitbit_active_zone_minutes_history", "min", "date"),
    ("active_minutes_total", "get_fitbit_active_minutes_history", "min", "date"),
    ("total_calories_kcal", "get_fitbit_total_calories_history", "kcal", "date"),
    ("active_energy_kcal", "get_fitbit_active_energy_burned_history", "kcal", "date"),
    ("floors", "get_fitbit_floors_history", "count", "date"),
)


def iso_date(value):
    if isinstance(value, str) and len(value) >= 10 and value[4:5] == "-" and value[7:8] == "-":
        return value[:10]
    if isinstance(value, dict):
        year, month, day = value.get("year"), value.get("month"), value.get("day")
        if all(isinstance(part, int) and not isinstance(part, bool) for part in (year, month, day)):
            if 1 <= month <= 12 and 1 <= day <= 31:
                return f"{year:04d}-{month:02d}-{day:02d}"
    return None


def _strings(value):
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _stage_totals(value):
    rows = []
    if not isinstance(value, list):
        return rows
    for item in value:
        if not isinstance(item, dict):
            continue
        rows.append({
            "type": _text(item.get("type")),
            "minutes": _num(item.get("minutes")),
            "count": _num(item.get("count")),
        })
    return rows


def _activity_levels(value):
    if not isinstance(value, dict):
        return {}
    levels = {}
    for key, minutes in value.items():
        number = _num(minutes)
        if isinstance(key, str) and key and number is not None:
            levels[key] = number
    return levels


def _zone_bounds(record):
    zones = record.get("zones") if isinstance(record, dict) else None
    rows = []
    if not isinstance(zones, list):
        return rows
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        if "duration_seconds" in zone and "min_bpm" not in zone:
            continue
        rows.append({
            "type": _text(zone.get("type")),
            "min_bpm": _num(zone.get("min_bpm")),
            "max_bpm": _num(zone.get("max_bpm")),
        })
    return rows


def _time_in_zone(record):
    zones = record.get("zones") if isinstance(record, dict) else None
    rows = []
    if not isinstance(zones, list):
        return rows
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        name = _text(zone.get("zone"))
        seconds = _num(zone.get("duration_seconds"))
        if name is None or seconds is None:
            continue
        rows.append({"zone": name, "duration_seconds": seconds})
    return rows


def normalize_sleep(record):
    if not isinstance(record, dict):
        return None
    wake = iso_date(record.get("local_wake_date"))
    if wake is None and _text(record.get("sleep_type")) is None and _num(record.get("minutes_asleep")) is None:
        return None
    return {
        "source": SOURCE,
        "date": wake,
        "start": _text(record.get("start")),
        "end": _text(record.get("end")),
        "local_start": _text(record.get("local_start")),
        "local_end": _text(record.get("local_end")),
        "sleep_type": _text(record.get("sleep_type")),
        "sleep_role": _text(record.get("sleep_role")),
        "main_sleep": record.get("main_sleep") if isinstance(record.get("main_sleep"), bool) else None,
        "minutes_in_sleep_period": _num(record.get("minutes_in_sleep_period")),
        "minutes_asleep": _num(record.get("minutes_asleep")),
        "minutes_awake": _num(record.get("minutes_awake")),
        "minutes_to_fall_asleep": _num(record.get("minutes_to_fall_asleep")),
        "minutes_after_wakeup": _num(record.get("minutes_after_wakeup")),
        "stages_status": _text(record.get("stages_status")),
        "sleep_stage_totals": _stage_totals(record.get("sleep_stage_totals")),
        "sleep_measurement_quality": _text(record.get("sleep_measurement_quality")),
        "quality_status": _text(record.get("quality_status")),
        "quality_flags": _strings(record.get("quality_flags")),
        "usable_for": _strings(record.get("usable_for")),
        "device": _text(record.get("device")),
        "platform": _text(record.get("platform")),
    }


def _dated_number(record, field, date_field="date"):
    if not isinstance(record, dict):
        return None
    return {
        "date": iso_date(record.get(date_field)),
        "value": _num(record.get(field)),
        "device": _text(record.get("device")),
        "platform": _text(record.get("platform")),
    }


def metric(name, value, unit, date, tool, freshness, device=None, platform=None):
    return {
        "source": SOURCE,
        "name": name,
        "value": value,
        "unit": unit,
        "date": date,
        "tool": tool,
        "freshness": freshness,
        "device": device,
        "platform": platform,
    }


def _sum(values):
    numbers = [value for value in values if value is not None]
    if not numbers:
        return None
    return _num(sum(numbers))


class HealthSource:
    def __init__(self, call_tool, now=None, clock=None):
        self.call_tool = call_tool
        self.now = now or (lambda: datetime.now(_zone()))
        self._clock = clock or time.monotonic
        self._cache = {}

    def _zone_now(self):
        current = self.now()
        zone = _zone()
        if current.tzinfo is None:
            return current.replace(tzinfo=zone)
        return current.astimezone(zone)

    def _window(self, days):
        end = self._zone_now().date()
        start = end - timedelta(days=days - 1)
        return start, end

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

    def _call(self, name, arguments):
        try:
            return self.call_tool(name, arguments), "ok"
        except Exception:
            return None, "unavailable"

    def today(self):
        cached = self._cached("today")
        if cached is not None:
            return cached
        return self._store("today", self._load_today())

    def summary(self):
        cached = self._cached("summary")
        if cached is not None:
            return cached
        return self._store("summary", self._load_range(SUMMARY_DAYS, "summary"))

    def trends(self):
        cached = self._cached("trends")
        if cached is not None:
            return cached
        return self._store("trends", self._load_range(TREND_DAYS, "trends"))

    def _load_today(self):
        today = self._zone_now().date().isoformat()
        tools = {}
        metrics = []
        sleep_rows, tools["get_fitbit_sleep"] = self._records("get_fitbit_sleep", {"limit": 5}, normalize_sleep)
        sleep = _prefer_sleep(sleep_rows)
        metrics.append(_sleep_metric(sleep, tools["get_fitbit_sleep"], today))
        metrics.extend(self._latest_metrics(today, tools))
        metrics.extend(self._day_metrics(today, tools))
        sample, tools["get_fitbit_heart_rate"] = self._records(
            "get_fitbit_heart_rate", {"limit": 1}, _heart_sample,
        )
        metrics.append(_from_point(
            "heart_rate_bpm", "bpm", "get_fitbit_heart_rate", sample, today, tools["get_fitbit_heart_rate"],
        ))
        zones, tools["get_fitbit_heart_rate_zones"] = self._records(
            "get_fitbit_heart_rate_zones", {"limit": 1}, _zone_record,
        )
        zone_row = _latest(zones)
        metrics.append(metric(
            "heart_rate_zone_bounds",
            zone_row.get("zones") if zone_row else None,
            "bpm_bounds",
            zone_row.get("date") if zone_row else None,
            "get_fitbit_heart_rate_zones",
            _fresh(tools["get_fitbit_heart_rate_zones"], zone_row.get("zones") if zone_row else None, zone_row.get("date") if zone_row else None, today),
        ))
        present = any(item["freshness"] == "present" for item in metrics)
        return {
            "available": present or any(status == "ok" for status in tools.values()),
            "reason": None if present or any(status == "ok" for status in tools.values()) else "FITBIT_UNAVAILABLE",
            "source": SOURCE,
            "timezone": TIMEZONE,
            "date": today,
            "generated_at": self._zone_now().isoformat(timespec="seconds"),
            "metrics": metrics,
            "sleep": sleep,
            "recovery": _recovery(metrics),
            "watchlist": _watchlist(metrics),
            "freshness": {"source": SOURCE, "tools": tools},
        }

    def _latest_metrics(self, today, tools):
        specs = (
            ("resting_heart_rate_bpm", "get_fitbit_resting_heart_rate", "bpm", "resting_heart_rate_bpm"),
            ("average_hrv_ms", "get_fitbit_hrv", "ms", "average_hrv_ms"),
            ("average_spo2_percent", "get_fitbit_oxygen_saturation", "percent", "average_spo2_percent"),
            ("breaths_per_minute", "get_fitbit_respiratory_rate", "breaths_per_minute", "breaths_per_minute"),
            ("difference_from_baseline_celsius", "get_fitbit_sleep_temperature", "celsius", "difference_from_baseline_celsius"),
            ("weight_pounds", "get_fitbit_weight", "lb", "weight_pounds"),
        )
        rows = []
        for name, tool, unit, field in specs:
            records, tools[tool] = self._records(tool, {"limit": 5}, lambda record, field=field: _dated_number(record, field))
            rows.append(_from_point(name, unit, tool, records, today, tools[tool]))
        return rows

    def _day_metrics(self, today, tools):
        specs = (
            ("steps", "get_fitbit_steps", "count", "steps"),
            ("distance_miles", "get_fitbit_distance", "mi", "distance_miles"),
            ("active_zone_minutes", "get_fitbit_active_zone_minutes", "min", "active_zone_minutes"),
            ("active_minutes_total", "get_fitbit_active_minutes", "min", "active_minutes_total"),
            ("total_calories_kcal", "get_fitbit_total_calories", "kcal", "total_calories_kcal"),
            ("active_energy_kcal", "get_fitbit_active_energy_burned", "kcal", "active_energy_kcal"),
            ("floors", "get_fitbit_floors", "count", "floors"),
        )
        rows = []
        for name, tool, unit, field in specs:
            payload, tools[tool] = self._call(tool, {"date": today})
            record = _one_record(payload, lambda item, field=field: _dated_number(item, field))
            value = record.get("value") if record else None
            if name == "active_minutes_total" and isinstance(payload, dict):
                levels = _activity_levels(payload.get("by_activity_level"))
            else:
                levels = None
            item = metric(
                name, value, unit, record.get("date") if record else None, tool,
                _fresh(tools[tool], value, record.get("date") if record else None, today),
                record.get("device") if record else None,
                record.get("platform") if record else None,
            )
            if levels:
                item["by_activity_level"] = levels
            if name == "active_zone_minutes" and isinstance(payload, dict):
                item["fat_burn_zone_minutes"] = _num(payload.get("fat_burn_zone_minutes"))
                item["cardio_zone_minutes"] = _num(payload.get("cardio_zone_minutes"))
                item["peak_zone_minutes"] = _num(payload.get("peak_zone_minutes"))
            rows.append(item)
        zone_payload, tools["get_fitbit_time_in_heart_rate_zone"] = self._call(
            "get_fitbit_time_in_heart_rate_zone", {"date": today},
        )
        zone_rows = _time_in_zone(zone_payload if isinstance(zone_payload, dict) else {})
        rows.append(metric(
            "time_in_heart_rate_zone",
            zone_rows or None,
            "seconds",
            iso_date(zone_payload.get("date")) if isinstance(zone_payload, dict) else None,
            "get_fitbit_time_in_heart_rate_zone",
            _fresh(
                tools["get_fitbit_time_in_heart_rate_zone"],
                zone_rows or None,
                iso_date(zone_payload.get("date")) if isinstance(zone_payload, dict) else None,
                today,
            ),
        ))
        return rows

    def _records(self, tool, arguments, normalize):
        payload, status = self._call(tool, arguments)
        if status != "ok":
            return [], status
        rows = []
        for record in history_rows(payload):
            item = normalize(record)
            if item is not None:
                rows.append(item)
        return rows, status

    def _load_range(self, days, kind):
        start, end = self._window(days)
        tools = {}
        series = {}
        any_ok = False
        for name, tool, unit, date_field in SERIES:
            payload, status = self._call(tool, {
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
            })
            tools[tool] = status
            if status != "ok":
                series[name] = {"available": False, "unit": unit, "points": []}
                continue
            any_ok = True
            points = []
            for record in history_rows(payload):
                if not isinstance(record, dict) or "raw_points" in record:
                    record = {key: value for key, value in record.items() if key != "raw_points"} if isinstance(record, dict) else None
                if not isinstance(record, dict):
                    continue
                civil = iso_date(record.get(date_field if name != "minutes_asleep" else "local_wake_date"))
                value = _num(record.get(name if name != "minutes_asleep" else "minutes_asleep"))
                if civil is None or value is None or not (start.isoformat() <= civil <= end.isoformat()):
                    continue
                points.append({"date": civil, "value": value, "source": SOURCE})
            points.sort(key=lambda item: item["date"])
            series[name] = {
                "available": True,
                "source": SOURCE,
                "unit": unit,
                "points": points,
                "latest": points[-1] if points else None,
                "total": _sum(point["value"] for point in points) if name in ADDITIVE else None,
                "sample_count": len(points),
            }
        zone_payload, zone_status = self._call("get_fitbit_time_in_heart_rate_zone_history", {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        })
        tools["get_fitbit_time_in_heart_rate_zone_history"] = zone_status
        zone_totals = {}
        if zone_status == "ok":
            any_ok = True
            for record in history_rows(zone_payload):
                for zone in _time_in_zone(record if isinstance(record, dict) else {}):
                    zone_totals[zone["zone"]] = _num((zone_totals.get(zone["zone"]) or 0) + zone["duration_seconds"])
        return {
            "available": any_ok,
            "reason": None if any_ok else "FITBIT_UNAVAILABLE",
            "source": SOURCE,
            "kind": kind,
            "timezone": TIMEZONE,
            "days": days,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "generated_at": self._zone_now().isoformat(timespec="seconds"),
            "series": series,
            "time_in_heart_rate_zone_seconds": {
                "available": zone_status == "ok",
                "source": SOURCE,
                "duration_seconds": zone_totals,
            },
            "freshness": {"source": SOURCE, "tools": tools},
        }


def _one_record(payload, normalize):
    rows = []
    for record in history_rows(payload):
        if isinstance(record, dict) and "raw_points" in record:
            record = {key: value for key, value in record.items() if key != "raw_points"}
        item = normalize(record)
        if item is not None:
            rows.append(item)
    return _latest(rows)


def _latest(rows):
    dated = [row for row in rows if row and row.get("date")]
    if dated:
        return sorted(dated, key=lambda row: row["date"])[-1]
    return rows[-1] if rows else None


def _prefer_sleep(rows):
    mains = [row for row in rows if row.get("sleep_role") == "main_sleep" or row.get("main_sleep") is True]
    return _latest(mains or rows)


def _fresh(status, value, date, today):
    if status != "ok":
        return "unavailable"
    if value is None:
        return "missing"
    if date and date != today:
        return "not_today"
    return "present"


def _from_point(name, unit, tool, rows, today, status):
    point = _latest(rows)
    value = point.get("value") if point else None
    date = point.get("date") if point else None
    return metric(
        name, value, unit, date, tool, _fresh(status, value, date, today),
        point.get("device") if point else None,
        point.get("platform") if point else None,
    )


def _sleep_metric(sleep, status, today):
    value = sleep.get("minutes_asleep") if sleep else None
    date = sleep.get("date") if sleep else None
    freshness = _fresh(status, value, date, today)
    if freshness == "not_today" and date:
        try:
            observed = datetime.fromisoformat(date).date()
            current = datetime.fromisoformat(today).date()
            if current - timedelta(days=1) <= observed <= current:
                freshness = "present"
        except ValueError:
            pass
    return metric(
        "minutes_asleep", value, "min", date, "get_fitbit_sleep", freshness,
        sleep.get("device") if sleep else None,
        sleep.get("platform") if sleep else None,
    )


def _heart_sample(record):
    point = _dated_number(record, "heart_rate_bpm", "local_date")
    return point


def _zone_record(record):
    if not isinstance(record, dict):
        return None
    return {"date": iso_date(record.get("date")), "zones": _zone_bounds(record)}


def _recovery(metrics):
    names = {
        "minutes_asleep",
        "resting_heart_rate_bpm",
        "average_hrv_ms",
        "average_spo2_percent",
        "breaths_per_minute",
        "difference_from_baseline_celsius",
    }
    return {
        "source": SOURCE,
        "series": "fitbit_nightly_hrv",
        "note": "Fitbit HRV is the Fitbit nightly series, not Polar H10 morning HRV.",
        "metrics": [item for item in metrics if item["name"] in names],
    }


def _watchlist(metrics):
    items = []
    for item in metrics:
        if item["freshness"] == "present":
            continue
        reason = {
            "unavailable": "UNAVAILABLE",
            "missing": "MISSING",
            "not_today": "NOT_TODAY",
        }.get(item["freshness"], "MISSING")
        items.append({
            "source": SOURCE,
            "name": item["name"],
            "reason": reason,
            "date": item.get("date"),
            "tool": item.get("tool"),
        })
    return items


def open_health(url=None):
    client = FitbitMcp(
        url or os.environ.get("FITBIT_MCP_URL", DEFAULT_URL),
        allowed_tools=HEALTH_TOOLS,
    )
    return HealthSource(client.call_tool)
