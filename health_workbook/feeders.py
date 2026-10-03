"""Push Fitbit and Macro App rows through the health workbook API.

Neither feeder opens the workbook file. A repeat of the same values does not
write. Logs stay in the caller; this module returns counts and field names.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from workouts.fitbit import history_rows

TIMEZONE = "America/Los_Angeles"
FITBIT_ACTOR = "fitbit-sync"
FITBIT_SOURCE = "FITBIT"
MACRO_ACTOR = "macro-sync"
MACRO_SOURCE = "MACRO_APP"
FITBIT_REFRESH = (
    "steps",
    "distance_mi",
    "active_kcal_fitbit",
    "azm_total",
    "hrv_ms",
    "rhr_bpm",
    "sleep_asleep_min",
    "fitbit_total_burn",
    "fitbit_active_burn",
    "fitbit_resting_burn",
)
MACRO_FIELDS = ("kcal_in", "protein_g", "carbs_g", "fat_g")
MEAL_FIELDS = ("kcal", "protein_g", "carbs_g", "fat_g")
TRAINING_FIELDS = (
    "date",
    "type",
    "start_local",
    "duration_min",
    "avg_hr",
    "calories",
    "distance_mi",
    "details",
    "burn_role",
)
FITBIT_TOOLS = (
    "get_fitbit_steps_history",
    "get_fitbit_distance_history",
    "get_fitbit_active_zone_minutes_history",
    "get_fitbit_total_calories_history",
    "get_fitbit_active_energy_burned_history",
    "get_fitbit_hrv_history",
    "get_fitbit_resting_heart_rate_history",
    "get_fitbit_sleep_history",
    "get_fitbit_weight_history",
    "get_fitbit_exercise_history",
)


class WorkbookClientError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


def zone():
    try:
        return ZoneInfo(TIMEZONE)
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=-8))


def sync_dates(today=None) -> list[str]:
    current = today or datetime.now(zone()).date()
    return [(current - timedelta(days=1)).isoformat(), current.isoformat()]


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        return None
    rounded = round(number, 2)
    if rounded == int(rounded):
        return int(rounded)
    return rounded


def _blank(value) -> bool:
    return value is None or value == ""


def _equal(left, right) -> bool:
    if left == right:
        return True
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    return False


def _day(value) -> str | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    return value[:10]


def _resting_burn(total, active):
    """Fitbit resting burn is total minus active when that difference is usable."""
    if total is None or active is None:
        return None
    derived = round(float(total) - float(active), 2)
    if derived < 0:
        return None
    return _num(derived)


def _local_parts(value):
    if not isinstance(value, str) or not value:
        return None, None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None, None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone())
    local = parsed.astimezone(zone())
    return local.date().isoformat(), local.strftime("%Y-%m-%d %H:%M")


def session_id(record: dict) -> str | None:
    start = record.get("start")
    if not isinstance(start, str) or not start:
        return None
    kind = record.get("type") if isinstance(record.get("type"), str) else ""
    end = record.get("end") if isinstance(record.get("end"), str) else ""
    return "fitbit:" + start + "|" + kind + "|" + end


def _latest(payload, day: str, key: str, stamp_key: str | None = None):
    chosen = None
    stamp = ""
    for row in history_rows(payload):
        if not isinstance(row, dict) or _day(row.get("date")) != day:
            continue
        number = _num(row.get(key))
        if number is None:
            continue
        marker = str(row.get(stamp_key) or "") if stamp_key else stamp + " "
        if chosen is None or marker >= stamp:
            chosen = number
            stamp = marker
    return chosen


def _steps(payload, day: str):
    for row in history_rows(payload):
        if not isinstance(row, dict) or _day(row.get("date")) != day:
            continue
        points = row.get("raw_points")
        if isinstance(points, list) and not points:
            return None
        return _num(row.get("steps"))
    return None


def _sleep_minutes(payload, day: str):
    minutes = []
    for row in history_rows(payload):
        if not isinstance(row, dict) or _day(row.get("local_wake_date")) != day:
            continue
        if row.get("main_sleep") not in (True, 1):
            continue
        number = _num(row.get("minutes_asleep"))
        if number is not None:
            minutes.append(number)
    if not minutes:
        return None
    return max(minutes)


def fitbit_daily_fields(snapshots: dict, day: str) -> dict:
    total = _latest(snapshots.get("get_fitbit_total_calories_history"), day, "total_calories_kcal")
    active = _latest(snapshots.get("get_fitbit_active_energy_burned_history"), day, "active_energy_kcal")
    fields = {
        "weight_lb": _latest(snapshots.get("get_fitbit_weight_history"), day, "weight_pounds", "physical_time"),
        "steps": _steps(snapshots.get("get_fitbit_steps_history"), day),
        "distance_mi": _latest(snapshots.get("get_fitbit_distance_history"), day, "distance_miles"),
        "active_kcal_fitbit": active,
        "azm_total": _latest(snapshots.get("get_fitbit_active_zone_minutes_history"), day, "active_zone_minutes"),
        "hrv_ms": _latest(snapshots.get("get_fitbit_hrv_history"), day, "average_hrv_ms"),
        "rhr_bpm": _latest(snapshots.get("get_fitbit_resting_heart_rate_history"), day, "resting_heart_rate_bpm"),
        "sleep_asleep_min": _sleep_minutes(snapshots.get("get_fitbit_sleep_history"), day),
        "fitbit_total_burn": total,
        "fitbit_active_burn": active,
        "fitbit_resting_burn": _resting_burn(total, active),
    }
    return {key: value for key, value in fields.items() if value is not None}


def fitbit_workouts(snapshots: dict, dates: list[str]) -> list[dict]:
    wanted = set(dates)
    workouts = []
    seen = set()
    for record in history_rows(snapshots.get("get_fitbit_exercise_history")):
        if not isinstance(record, dict):
            continue
        ident = session_id(record)
        day, start_local = _local_parts(record.get("start"))
        if ident is None or day not in wanted or ident in seen:
            continue
        seen.add(ident)
        body = {
            "date": day,
            "session_id": ident,
            "source": FITBIT_SOURCE,
            "burn_role": "do_not_sum",
            "start_local": start_local,
            "type": record.get("type") if isinstance(record.get("type"), str) else None,
            "duration_min": _num(record.get("duration_minutes")),
            "avg_hr": _num(record.get("average_heart_rate_bpm")),
            "calories": _num(record.get("calories")),
            "distance_mi": _num(record.get("distance_miles")),
            "details": record.get("exercise") if isinstance(record.get("exercise"), str) else None,
        }
        workouts.append({key: value for key, value in body.items() if value is not None})
    return workouts


def _owned_changes(existing: dict | None, incoming: dict, actor: str, refresh: tuple[str, ...]) -> tuple[dict, list[str]]:
    changes = {}
    conflicts = []
    for key, value in incoming.items():
        current = None if existing is None else existing.get(key)
        if existing is not None and _equal(current, value):
            continue
        if key == "weight_lb" and existing is not None and not _blank(current) and (existing.get("updated_by") or "") != actor:
            conflicts.append(key)
            continue
        if key in refresh or key == "weight_lb" or _blank(current):
            changes[key] = value
    return changes, conflicts


def counted_macro_entries(entries: list[dict]) -> list[dict]:
    aggregate_dates = {entry.get("entry_date") for entry in entries if entry.get("nutrition_accounting") == "aggregate"}
    counted = []
    for entry in entries:
        if entry.get("category") == "supplement":
            continue
        accounting = entry.get("nutrition_accounting") or "itemized"
        if accounting == "reference":
            continue
        if accounting == "covered_by_aggregate" and entry.get("entry_date") in aggregate_dates:
            continue
        counted.append(entry)
    return counted


def macro_meal(entry: dict) -> dict | None:
    day = _day(entry.get("entry_date"))
    food = entry.get("food_name_snapshot")
    if day is None or not isinstance(food, str) or not food.strip():
        return None
    logged = entry.get("logged_time")
    if isinstance(logged, str) and logged.strip():
        meal_time = logged.strip()
    else:
        ident = entry.get("id")
        if not isinstance(ident, str) or not ident:
            return None
        meal_time = "untimed:" + ident
    body = {
        "date": day,
        "time": meal_time,
        "food": food.strip(),
        "external_id": entry.get("id") if isinstance(entry.get("id"), str) else None,
        "kcal": _num(entry.get("kcal")),
        "protein_g": _num(entry.get("protein_g")),
        "carbs_g": _num(entry.get("carbs_g")),
        "fat_g": _num(entry.get("fat_g")),
    }
    return {key: value for key, value in body.items() if value is not None}


def macro_totals(entries: list[dict], day: str) -> dict:
    day_entries = [entry for entry in counted_macro_entries(entries) if entry.get("entry_date") == day]
    if not day_entries:
        return {}
    totals = {}
    mapping = {"kcal": "kcal_in", "protein_g": "protein_g", "carbs_g": "carbs_g", "fat_g": "fat_g"}
    for source, target in mapping.items():
        if any(entry.get(source) is None for entry in day_entries):
            continue
        totals[target] = _num(sum(float(entry[source]) for entry in day_entries))
    return {key: value for key, value in totals.items() if value is not None}


def load_macro_entries(path: str, dates: list[str]) -> list[dict]:
    connection = sqlite3.connect("file:" + path + "?mode=ro", uri=True, timeout=10)
    try:
        connection.row_factory = sqlite3.Row
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "imported_entry_details" in tables:
            accounting = "COALESCE(i.nutrition_accounting, 'itemized')"
            join = "LEFT JOIN imported_entry_details i ON i.entry_id = e.id"
        else:
            accounting = "'itemized'"
            join = ""
        placeholders = ",".join("?" for _ in dates)
        query = f"""SELECT e.id, e.entry_date, e.logged_time, e.food_name_snapshot,
            e.kcal, e.protein_g, e.carbs_g, e.fat_g, e.category,
            {accounting} AS nutrition_accounting
            FROM food_entries e
            {join}
            WHERE e.entry_date IN ({placeholders})
            ORDER BY e.entry_date, e.logged_time, e.created_at, e.id"""
        return [dict(row) for row in connection.execute(query, dates)]
    finally:
        connection.close()


class WorkbookClient:
    def __init__(self, base_url: str, token: str, timeout_s: float = 30.0):
        parsed = urlsplit(base_url)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
            raise WorkbookClientError(0, "workbook_url")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_s = timeout_s

    def rows(self, sheet: str, **filters) -> list[dict]:
        query = urlencode({"sheet": sheet, "limit": 500, **{key: value for key, value in filters.items() if value is not None}})
        body = self._json("GET", "/v1/rows?" + query, None)
        rows = body.get("rows")
        return rows if isinstance(rows, list) else []

    def post(self, sheet: str, payload: dict) -> dict:
        return self._json("POST", "/v1/" + sheet, payload)

    def patch(self, sheet: str, row_id: str, fields: dict, reason: str, source: str, actor: str, recorded_at: str) -> dict:
        return self._json("PATCH", "/v1/" + sheet + "/" + row_id, {
            "source": source,
            "updated_by": actor,
            "recorded_at": recorded_at,
            "reason": reason,
            "fields": fields,
        })

    def _json(self, method: str, path: str, payload: dict | None) -> dict:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Authorization": "Bearer " + self.token}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            code = "http_error"
            try:
                parsed = json.loads(error.read().decode())
                if isinstance(parsed, dict) and isinstance(parsed.get("error"), str):
                    code = parsed["error"]
            except (OSError, UnicodeError, json.JSONDecodeError):
                code = "http_error"
            finally:
                error.close()
            raise WorkbookClientError(error.code, code) from None


def _empty(feeder: str) -> dict:
    return {
        "feeder": feeder,
        "daily_appended": 0,
        "daily_patched": 0,
        "daily_exists": 0,
        "daily_conflicts": 0,
        "fields_written": [],
        "conflicts": [],
        "errors": 0,
    }


def _remember(result: dict, names: list[str]) -> None:
    have = set(result["fields_written"])
    have.update(names)
    result["fields_written"] = sorted(have)


def _one_row(rows: list[dict], day: str) -> dict | None:
    matched = [row for row in rows if _day(row.get("date")) == day]
    if len(matched) > 1:
        raise WorkbookClientError(409, "duplicate_day")
    return matched[0] if matched else None


def _apply_daily(workbook: WorkbookClient, day: str, incoming: dict, actor: str, source: str, recorded_at: str, reason: str, result: dict) -> None:
    if not incoming:
        result["daily_exists"] += 1
        return
    try:
        existing = _one_row(workbook.rows("daily", date=day), day)
        changes, conflicts = _owned_changes(existing, incoming, actor, FITBIT_REFRESH if actor == FITBIT_ACTOR else MACRO_FIELDS)
    except WorkbookClientError:
        result["errors"] += 1
        return
    result["conflicts"].extend(day + ":" + name for name in conflicts)
    result["daily_conflicts"] += len(conflicts)
    if not changes:
        result["daily_exists"] += 1
        return
    body = {
        "date": day,
        "source": source,
        "updated_by": actor,
        "recorded_at": recorded_at,
        **changes,
    }
    try:
        if existing is None:
            workbook.post("daily", body)
            result["daily_appended"] += 1
        else:
            workbook.patch("daily", existing["row_id"], changes, reason, source, actor, recorded_at)
            result["daily_patched"] += 1
        _remember(result, list(changes))
    except WorkbookClientError as error:
        if error.code == "conflict":
            result["daily_conflicts"] += 1
            result["conflicts"].append(day + ":row")
            return
        result["errors"] += 1


def sync_fitbit(fetch, workbook: WorkbookClient, dates: list[str], recorded_at: str) -> dict:
    result = _empty("fitbit")
    result.update({
        "workouts_appended": 0,
        "workouts_patched": 0,
        "workouts_exists": 0,
        "workouts_conflicts": 0,
        "workouts_written": 0,
        "failed_tools": [],
    })
    snapshots = {}
    for name in FITBIT_TOOLS:
        try:
            snapshots[name] = fetch(name, dates)
        except Exception:
            snapshots[name] = None
            result["failed_tools"].append(name)
            result["errors"] += 1
    for day in dates:
        _apply_daily(workbook, day, fitbit_daily_fields(snapshots, day), FITBIT_ACTOR, FITBIT_SOURCE, recorded_at, FITBIT_ACTOR, result)
    for workout in fitbit_workouts(snapshots, dates):
        _apply_training(workbook, workout, recorded_at, result)
    result["workouts_written"] = result["workouts_appended"] + result["workouts_patched"]
    return result


def _apply_training(workbook: WorkbookClient, workout: dict, recorded_at: str, result: dict) -> None:
    try:
        rows = workbook.rows("training", session_id=workout["session_id"])
    except WorkbookClientError:
        result["errors"] += 1
        return
    existing = next((row for row in rows if row.get("source") == FITBIT_SOURCE and row.get("session_id") == workout["session_id"]), None)
    changes = {}
    if existing is not None:
        for key in TRAINING_FIELDS:
            if key not in workout or _equal(existing.get(key), workout[key]):
                continue
            actor = existing.get("updated_by") or ""
            if actor not in ("", FITBIT_ACTOR):
                result["workouts_conflicts"] += 1
                result["conflicts"].append("training")
                return
            changes[key] = workout[key]
        if not changes:
            result["workouts_exists"] += 1
            return
    payload = {
        **workout,
        "updated_by": FITBIT_ACTOR,
        "recorded_at": recorded_at,
    }
    try:
        if existing is None:
            workbook.post("training", payload)
            result["workouts_appended"] += 1
        else:
            workbook.patch("training", existing["row_id"], changes, FITBIT_ACTOR, FITBIT_SOURCE, FITBIT_ACTOR, recorded_at)
            result["workouts_patched"] += 1
    except WorkbookClientError as error:
        if error.code == "conflict":
            result["workouts_conflicts"] += 1
            result["conflicts"].append("training")
            return
        result["errors"] += 1


def sync_macro(entries: list[dict], workbook: WorkbookClient, dates: list[str], recorded_at: str) -> dict:
    result = _empty("macro")
    result.update({
        "meals_appended": 0,
        "meals_patched": 0,
        "meals_exists": 0,
        "meals_conflicts": 0,
        "meals_skipped": 0,
        "meals_written": 0,
        "daily_totals_written": [],
    })
    wanted = set(dates)
    seen = set()
    for entry in counted_macro_entries(entries):
        if entry.get("entry_date") not in wanted:
            continue
        meal = macro_meal(entry)
        if meal is None:
            result["meals_skipped"] += 1
            continue
        identity = (meal["date"], meal["time"], meal["food"])
        if identity in seen:
            result["meals_skipped"] += 1
            continue
        seen.add(identity)
        _apply_meal(workbook, meal, recorded_at, result)
    for day in dates:
        totals = macro_totals(entries, day)
        _apply_daily(workbook, day, totals, MACRO_ACTOR, MACRO_SOURCE, recorded_at, MACRO_ACTOR, result)
        if totals:
            result["daily_totals_written"] = sorted(set(result["daily_totals_written"]) | set(totals))
    result["meals_written"] = result["meals_appended"] + result["meals_patched"]
    result["daily_totals_written"] = [name for name in result["fields_written"] if name in MACRO_FIELDS]
    return result


def _apply_meal(workbook: WorkbookClient, meal: dict, recorded_at: str, result: dict) -> None:
    try:
        rows = workbook.rows("meals", date=meal["date"])
    except WorkbookClientError:
        result["errors"] += 1
        return
    existing = next((
        row for row in rows
        if row.get("time") == meal["time"] and row.get("food") == meal["food"]
    ), None)
    changes = {}
    if existing is not None:
        for key in MEAL_FIELDS:
            if key not in meal or _equal(existing.get(key), meal[key]):
                continue
            if (existing.get("updated_by") or "") != MACRO_ACTOR:
                result["meals_conflicts"] += 1
                result["conflicts"].append("meal")
                return
            changes[key] = meal[key]
        if not changes:
            result["meals_exists"] += 1
            return
    payload = {
        "date": meal["date"],
        "time": meal["time"],
        "food": meal["food"],
        "source": MACRO_SOURCE,
        "updated_by": MACRO_ACTOR,
        "recorded_at": recorded_at,
        **{key: meal[key] for key in ("kcal", "protein_g", "carbs_g", "fat_g", "external_id") if key in meal},
    }
    try:
        if existing is None:
            workbook.post("meals", payload)
            result["meals_appended"] += 1
        else:
            workbook.patch("meals", existing["row_id"], changes, MACRO_ACTOR, MACRO_SOURCE, MACRO_ACTOR, recorded_at)
            result["meals_patched"] += 1
    except WorkbookClientError as error:
        if error.code == "conflict":
            result["meals_conflicts"] += 1
            result["conflicts"].append("meal")
            return
        result["errors"] += 1
