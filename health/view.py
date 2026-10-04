"""Read-only Mission Control view of the loopback health workbook service."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from pathlib import Path

from health_workbook.feeders import WorkbookClient, WorkbookClientError, zone

DEFAULT_URL = "http://127.0.0.1:5052"
DEFAULT_ENV = "/home/KA_PI/syzygy-runtime/health-workbook.env"
READ_KEY = "HEALTH_WORKBOOK_READ_TOKEN"
WINDOW_DAYS = 30
RECENT_LIMIT = 16
ACTORS = {
    "fitbit-sync": "FITBIT",
    "withings-sync": "WITHINGS",
    "polar-sync": "POLAR_H10",
    "polar-h10-sync": "POLAR_H10",
    "manual": "MANUAL",
}
DAILY_CARDS = (
    ("weight", "Weight", "weight_lb", "lb"),
    ("body_fat", "Body fat", "body_fat_pct", "%"),
    ("resting_hr", "Resting heart rate", "rhr_bpm", "bpm"),
    ("hrv", "HRV", "hrv_ms", "ms"),
    ("sleep", "Sleep", "sleep_asleep_min", "min"),
    ("steps", "Steps", "steps", None),
    ("active_calories", "Active calories", "active_kcal_fitbit", "kcal"),
    ("total_burn", "Total burn", "fitbit_total_burn", "kcal"),
)
ACTIVE_FALLBACK = "fitbit_active_burn"
DAILY_TRENDS = (
    ("weight", "Weight", "weight_lb", "lb"),
    ("body_fat", "Body fat", "body_fat_pct", "%"),
    ("hrv", "HRV", "hrv_ms", "ms"),
    ("resting_hr", "Resting heart rate", "rhr_bpm", "bpm"),
    ("sleep", "Sleep", "sleep_asleep_min", "min"),
)
RECENT_METRICS = {"weight", "body_fat_pct", "blood_pressure", "glucose", "blood_glucose"}
GLUCOSE_METRICS = {"glucose", "blood_glucose"}


def read_token(environ: dict | None = None, env_file: str | None = None) -> str:
    env = os.environ if environ is None else environ
    token = str(env.get(READ_KEY) or "").strip()
    if token:
        return token
    path = Path(env_file or env.get("HEALTH_WORKBOOK_ENV") or DEFAULT_ENV)
    if path.is_symlink() or not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(READ_KEY + "="):
            return line.split("=", 1)[1].strip()
    return ""


def source_label(source, updated_by) -> str | None:
    if isinstance(source, str) and source.strip():
        return source.strip()
    actor = str(updated_by or "").strip()
    if not actor:
        return None
    mapped = ACTORS.get(actor.casefold())
    if mapped:
        return mapped
    if actor.casefold() in {"manual", "kja"}:
        return "MANUAL"
    return actor


def build_health(daily_rows: list, measurement_rows: list, today: date) -> dict:
    daily = [_row(row) for row in daily_rows if isinstance(row, dict)]
    measurements = [_row(row) for row in measurement_rows if isinstance(row, dict)]
    cards = [_daily_card(card, daily) for card in DAILY_CARDS]
    cards.append(_pressure_card(measurements))
    cards.append(_glucose_card(measurements))
    # Active calories fall back to the Fitbit active-burn column when the primary cell is empty.
    active = next(card for card in cards if card["id"] == "active_calories")
    if not active["present"]:
        fallback = _latest_daily(daily, ACTIVE_FALLBACK)
        if fallback is not None:
            day, value, source = fallback
            active.update({"present": True, "value": value, "date": day, "source": source})
    return {
        "available": True,
        "reason": None,
        "source": "health-workbook",
        "from": (today - timedelta(days=WINDOW_DAYS - 1)).isoformat(),
        "to": today.isoformat(),
        "cards": cards,
        "recent": _recent(measurements),
        "trends": _trends(daily, measurements, today),
    }


class HealthView:
    def __init__(self, client: WorkbookClient, today: date | None = None):
        self.client = client
        self.today = today

    def summary(self) -> dict:
        today = self.today or datetime.now(zone()).date()
        start = (today - timedelta(days=WINDOW_DAYS - 1)).isoformat()
        end = today.isoformat()
        daily = self.client.rows("daily", **{"from": start, "to": end})
        measurements = self.client.rows("measurements", **{"from": start, "to": end})
        return build_health(daily, measurements, today)


def open_health(environ: dict | None = None) -> HealthView | None:
    env = os.environ if environ is None else environ
    token = read_token(env)
    if not token:
        return None
    client = WorkbookClient(str(env.get("HEALTH_WORKBOOK_URL") or DEFAULT_URL), token)
    return HealthView(client)


def _row(row: dict) -> dict:
    return row


def _present(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _day(value) -> str:
    text = str(value or "")
    return text[:10]


def _latest_daily(rows: list[dict], field: str):
    chosen = None
    for row in rows:
        day = _day(row.get("date"))
        value = row.get(field)
        if len(day) != 10 or not _present(value):
            continue
        if chosen is None or day >= chosen[0]:
            chosen = (day, value, source_label(None, row.get("updated_by")))
    return chosen


def _daily_card(spec, rows: list[dict]) -> dict:
    card_id, label, field, unit = spec
    found = _latest_daily(rows, field)
    card = {
        "id": card_id,
        "label": label,
        "present": found is not None,
        "value": None if found is None else found[1],
        "value2": None,
        "unit": unit,
        "source": None if found is None else found[2],
        "device": None,
        "date": None if found is None else found[0],
    }
    return card


def _measurement_time(row: dict) -> str:
    return str(row.get("timestamp") or "")


def _pressure_card(rows: list[dict]) -> dict:
    chosen = None
    for row in rows:
        if row.get("metric") != "blood_pressure":
            continue
        if not _present(row.get("value")) or not _present(row.get("value2")):
            continue
        if chosen is None or _measurement_time(row) >= _measurement_time(chosen):
            chosen = row
    return _measurement_card("blood_pressure", "Blood pressure", "mmHg", chosen, paired=True)


def _glucose_card(rows: list[dict]) -> dict:
    chosen = None
    for row in rows:
        if row.get("metric") not in GLUCOSE_METRICS:
            continue
        if not _present(row.get("value")):
            continue
        if chosen is None or _measurement_time(row) >= _measurement_time(chosen):
            chosen = row
    unit = None if chosen is None else chosen.get("unit") or None
    return _measurement_card("glucose", "Blood glucose", unit, chosen, paired=False)


def _measurement_card(card_id: str, label: str, unit, row: dict | None, paired: bool) -> dict:
    if row is None:
        return {
            "id": card_id,
            "label": label,
            "present": False,
            "value": None,
            "value2": None,
            "unit": unit,
            "source": None,
            "device": None,
            "date": None,
        }
    return {
        "id": card_id,
        "label": label,
        "present": True,
        "value": row.get("value"),
        "value2": row.get("value2") if paired else None,
        "unit": unit or row.get("unit"),
        "source": source_label(row.get("source"), row.get("updated_by")),
        "device": row.get("device") or None,
        "date": _day(row.get("timestamp")),
    }


def _recent(rows: list[dict]) -> list[dict]:
    selected = []
    for row in rows:
        metric = row.get("metric")
        if metric not in RECENT_METRICS or not _present(row.get("value")):
            continue
        if metric == "blood_pressure" and not _present(row.get("value2")):
            continue
        selected.append({
            "timestamp": _measurement_time(row),
            "date": _day(row.get("timestamp")),
            "metric": metric,
            "value": row.get("value"),
            "value2": row.get("value2") if metric == "blood_pressure" else None,
            "unit": row.get("unit") or ("mmHg" if metric == "blood_pressure" else None),
            "source": source_label(row.get("source"), row.get("updated_by")),
            "device": row.get("device") or None,
        })
    selected.sort(key=lambda item: item["timestamp"], reverse=True)
    return selected[:RECENT_LIMIT]


def _trends(daily: list[dict], measurements: list[dict], today: date) -> dict:
    trends = {}
    for trend_id, label, field, unit in DAILY_TRENDS:
        points = []
        for row in daily:
            day = _day(row.get("date"))
            value = row.get(field)
            if len(day) != 10 or not _present(value):
                continue
            points.append({
                "date": day,
                "value": value,
                "unit": unit,
                "source": source_label(None, row.get("updated_by")),
            })
        points.sort(key=lambda item: item["date"])
        trends[trend_id] = {"label": label, "unit": unit, "days_7": _cut(points, today, 7), "days_30": _cut(points, today, 30)}
    pressure = []
    glucose = []
    for row in measurements:
        day = _day(row.get("timestamp"))
        if len(day) != 10:
            continue
        metric = row.get("metric")
        if metric == "blood_pressure" and _present(row.get("value")) and _present(row.get("value2")):
            pressure.append({
                "date": day,
                "timestamp": _measurement_time(row),
                "systolic": row.get("value"),
                "diastolic": row.get("value2"),
                "unit": "mmHg",
                "source": source_label(row.get("source"), row.get("updated_by")),
                "device": row.get("device") or None,
            })
        elif metric in GLUCOSE_METRICS and _present(row.get("value")):
            glucose.append({
                "date": day,
                "timestamp": _measurement_time(row),
                "value": row.get("value"),
                "unit": row.get("unit") or None,
                "source": source_label(row.get("source"), row.get("updated_by")),
                "device": row.get("device") or None,
            })
    pressure.sort(key=lambda item: item["timestamp"])
    glucose.sort(key=lambda item: item["timestamp"])
    trends["blood_pressure"] = {
        "label": "Blood pressure",
        "unit": "mmHg",
        "days_7": _cut(pressure, today, 7),
        "days_30": _cut(pressure, today, 30),
    }
    trends["glucose"] = {
        "label": "Blood glucose",
        "unit": None,
        "days_7": _cut(glucose, today, 7),
        "days_30": _cut(glucose, today, 30),
    }
    return trends


def _cut(points: list[dict], today: date, days: int) -> list[dict]:
    start = (today - timedelta(days=days - 1)).isoformat()
    end = today.isoformat()
    return [point for point in points if start <= point["date"] <= end]


__all__ = [
    "HealthView",
    "WorkbookClientError",
    "build_health",
    "open_health",
    "read_token",
    "source_label",
]
