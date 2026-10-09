"""Serialized workbook writes: lock, backup, validate, replace, audit."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from health_workbook import formulas
from health_workbook.mutate import (
    WorkbookWriteError,
    _same,
    add_measurements_sheet,
    append_row,
    canonical_row_id,
    find_row,
    patch_row,
    row_values,
)
from health_workbook.workbook import TABLE_HEADERS, inspect_workbook

WRITABLE = ("Measurements", "Daily", "Meals", "Training", "Lifts", "Notes")
READ_ONLY = ("README", "Weight Trend", "Deficit Bank")
IDENTITY = {
    "Measurements": ("source", "external_id"),
    "Daily": ("date",),
    "Meals": ("date", "time", "food"),
    "Training": ("session_id", "source"),
    "Lifts": ("date", "exercise"),
    "Notes": ("datetime", "category", "note_paraphrased"),
}
OPTIONAL_IDENTITY = {"Measurements": {"external_id"}}
ATTRIBUTION = ("source", "updated_by", "recorded_at")
REQUIRED_PATCH = ("source", "updated_by", "recorded_at", "reason")
DATE_EXACT = {"date"}


class WriteFault(RuntimeError):
    """Injected failure used by tests."""


class WriteStore:
    def __init__(self, workbook_path: Path, fault: str | None = None):
        self.workbook_path = workbook_path
        self.fault = fault
        self.db_path = workbook_path.parent / "health-workbook-audit.sqlite3"
        self.backup_dir = workbook_path.parent / "backups"

    def append(self, record: dict) -> dict:
        return self.append_sheet("Measurements", record)

    def correct(self, row_id: str, record: dict) -> dict:
        return self.correct_sheet("Measurements", row_id, record)

    def migrate_measurements(self) -> dict:
        with self._locked() as connection:
            current = self._read()
            book = inspect_workbook(current)
            sheet = book.sheets.get("Measurements")
            if sheet is not None:
                if sheet.headers != list(TABLE_HEADERS["Measurements"]):
                    raise WorkbookWriteError("measurements_headers", "Measurements header does not match")
                return {"result": "present", "sheet": "Measurements", "backup": None}
            new_bytes = add_measurements_sheet(current)
            self._check(new_bytes, "Measurements")
            backup = self._commit(connection, current, new_bytes, {
                "action": "migrate",
                "sheet": "Measurements",
                "row_id": None,
                "actor": "migration",
                "source": "health-workbook",
                "reason": "add Measurements sheet",
                "external_id": None,
                "before_json": None,
                "after_json": json.dumps({"headers": list(TABLE_HEADERS["Measurements"])}),
            })
            return {"result": "migrated", "sheet": "Measurements", "backup": backup.name}

    def migrate_schema_v2(self) -> dict:
        """Daily.day_status + formula-driven deficit logic + Meals footer formulas. Idempotent."""
        with self._locked() as connection:
            current = self._read()
            new_bytes = formulas.migrate_v2(current)
            if new_bytes is current:
                return {"result": "present", "schema_version": formulas.schema_version(current), "backup": None}
            self._check(new_bytes, "Daily")
            backup = self._commit(connection, current, new_bytes, {
                "action": "migrate",
                "sheet": "Daily",
                "row_id": None,
                "actor": "migration",
                "source": "health-workbook",
                "reason": "schema v2: Daily.day_status, formula-driven deficit/under-1800, Meals footer formulas",
                "external_id": None,
                "before_json": json.dumps({"schema_version": formulas.schema_version(current)}),
                "after_json": json.dumps({"schema_version": formulas.SCHEMA_VERSION, "daily_added_header": formulas.STATUS}),
            })
            return {"result": "migrated", "schema_version": formulas.SCHEMA_VERSION, "backup": backup.name}

    def append_sheet(self, sheet_name: str, record: dict) -> dict:
        _reject_readonly(sheet_name)
        values = _sheet_values(sheet_name, record, _append_required(sheet_name))
        with self._locked() as connection:
            current = self._read()
            _guard_daily(current, sheet_name, values)
            existing = _existing_row(current, sheet_name, values)
            if existing:
                if _payload_matches(current, sheet_name, existing, values):
                    return {"result": "exists", "row_id": existing, "sheet": sheet_name, "backup": None}
                raise WorkbookWriteError(
                    "conflict",
                    f"{sheet_name} already has a different row for this identity. Patch row_id {existing}.",
                    existing,
                )
            new_bytes, row_id = append_row(current, sheet_name, _cell_values(sheet_name, values))
            self._check(new_bytes, sheet_name)
            backup = self._commit(connection, current, new_bytes, _audit("append", sheet_name, row_id, values, None, values))
            return {"result": "appended", "row_id": row_id, "sheet": sheet_name, "backup": backup.name}

    def correct_sheet(self, sheet_name: str, row_id: str, record: dict) -> dict:
        _reject_readonly(sheet_name)
        _require(record, REQUIRED_PATCH)
        fields = record.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise WorkbookWriteError("missing_field", "fields must include the corrected values")
        headers = set(TABLE_HEADERS[sheet_name])
        if sheet_name == "Daily":
            headers.add(formulas.STATUS)
        unknown = set(fields).difference(headers)
        if unknown:
            raise WorkbookWriteError("unknown_field", "Unknown field: " + ", ".join(sorted(unknown)))
        attribution = _sheet_values(sheet_name, {**fields, **record}, ATTRIBUTION)
        changes = {key: attribution[key] for key in fields if key in attribution}
        for key, value in fields.items():
            if value in (None, ""):
                changes[key] = None
        # day_status is bookkeeping, not data ownership: a status-only PATCH keeps the row's updated_by
        # (feeders use updated_by to decide who owns weight_lb). The audit log still records the actor.
        status_only = set(fields) == {formulas.STATUS}
        for key in ("updated_by", "recorded_at", "source"):
            if key in headers and not (status_only and key == "updated_by"):
                changes[key] = attribution[key]
        row_id = canonical_row_id(sheet_name, row_id)
        with self._locked() as connection:
            current = self._read()
            _guard_daily(current, sheet_name, {key: fields[key] for key in fields})
            probe = {**row_values(current, sheet_name, row_id), **changes}
            existing = _existing_row(current, sheet_name, probe)
            if existing and existing != row_id:
                raise WorkbookWriteError("conflict", f"{sheet_name} identity belongs to another row.", existing)
            new_bytes, before = patch_row(current, sheet_name, row_id, changes)
            self._check(new_bytes, sheet_name)
            after = {**before, **changes}
            backup = self._commit(connection, current, new_bytes, _audit(
                "correct", sheet_name, row_id, attribution, before, after, record["reason"].strip(),
            ))
            return {"result": "updated", "row_id": row_id, "sheet": sheet_name, "backup": backup.name}

    def audit_entries(self) -> list[dict]:
        if not self.db_path.exists():
            return []
        connection = sqlite3.connect(self.db_path, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT id, at, action, sheet, row_id, actor, source, reason, external_id, before_json, after_json, backup_name, workbook_sha FROM audit ORDER BY id"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def _commit(self, connection: sqlite3.Connection, current: bytes, new_bytes: bytes, audit: dict) -> Path:
        if self.fault == "before_replace":
            raise WriteFault("before_replace")
        backup = self._backup(current)
        replaced = False
        try:
            self._replace(new_bytes)
            replaced = True
            if self.fault == "after_replace":
                raise WriteFault("after_replace")
            written = self._read()
            if sha256(written).digest() != sha256(new_bytes).digest():
                raise WriteFault("replace_mismatch")
            self._check(written, audit["sheet"])
            if self.fault == "audit":
                raise WriteFault("audit")
            connection.execute(
                """INSERT INTO audit(at, action, sheet, row_id, actor, source, reason, external_id, before_json, after_json, backup_name, workbook_sha)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _stamp(),
                    audit["action"],
                    audit["sheet"],
                    audit["row_id"],
                    audit["actor"],
                    audit["source"],
                    audit["reason"],
                    audit["external_id"],
                    audit["before_json"],
                    audit["after_json"],
                    backup.name,
                    sha256(written).hexdigest(),
                ),
            )
        except Exception:
            if replaced:
                self._replace(backup.read_bytes())
            raise
        return backup

    def _check(self, data: bytes, sheet_name: str) -> None:
        book = inspect_workbook(data)
        sheet = book.sheets.get(sheet_name)
        accepted = [list(TABLE_HEADERS[sheet_name])]
        if sheet_name == "Daily":
            accepted.append(list(TABLE_HEADERS[sheet_name]) + [formulas.STATUS])
        if sheet is None or sheet.headers not in accepted:
            code = "measurements_missing" if sheet_name == "Measurements" else "sheet_missing"
            raise WorkbookWriteError(code, f"{sheet_name} sheet is missing or invalid")

    def _read(self) -> bytes:
        return self.workbook_path.read_bytes()

    def _backup(self, current: bytes) -> Path:
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        path = self.backup_dir / f"{_stamp()}-{sha256(current).hexdigest()}.xlsx"
        with path.open("wb") as stream:
            stream.write(current)
            stream.flush()
            os.fsync(stream.fileno())
        return path

    def _replace(self, data: bytes) -> None:
        directory = self.workbook_path.parent
        descriptor, name = tempfile.mkstemp(dir=directory, prefix=".workbook-", suffix=".tmp")
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.workbook_path)
        finally:
            if temporary.exists():
                temporary.unlink()

    @contextmanager
    def _locked(self):
        self._ensure_schema()
        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _ensure_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=30)
        try:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY,
                    at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    sheet TEXT NOT NULL,
                    row_id TEXT,
                    actor TEXT NOT NULL,
                    source TEXT NOT NULL,
                    reason TEXT,
                    external_id TEXT,
                    before_json TEXT,
                    after_json TEXT,
                    backup_name TEXT,
                    workbook_sha TEXT NOT NULL
                )"""
            )
            connection.commit()
        finally:
            connection.close()


def _append_required(sheet_name: str) -> tuple[str, ...]:
    identity = tuple(key for key in IDENTITY[sheet_name] if key not in OPTIONAL_IDENTITY.get(sheet_name, ()))
    required = []
    for key in (*ATTRIBUTION, *identity):
        if key not in required:
            required.append(key)
    if sheet_name == "Measurements":
        for key in ("timestamp", "metric"):
            if key not in required:
                required.append(key)
    if sheet_name == "Training" and "date" not in required:
        required.append("date")
    return tuple(required)


def _sheet_values(sheet_name: str, record: dict, required: tuple[str, ...]) -> dict:
    _require(record, required)
    headers = set(TABLE_HEADERS[sheet_name])
    if sheet_name == "Daily":
        headers.add(formulas.STATUS)
    allowed = headers | {"source", "updated_by", "recorded_at", "fields", "reason", "external_id"}
    unknown = set(record).difference(allowed)
    if unknown:
        raise WorkbookWriteError("unknown_field", "Unknown field: " + ", ".join(sorted(unknown)))
    values = {}
    extra = (formulas.STATUS,) if sheet_name == "Daily" else ()
    for key in (*TABLE_HEADERS[sheet_name], *extra, *ATTRIBUTION):
        if key not in record or record[key] in (None, ""):
            continue
        value = record[key]
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise WorkbookWriteError("invalid_value", f"{key} must be text or a number")
        values[key] = value.strip() if isinstance(value, str) else value
    _validate_dates(sheet_name, values)
    if formulas.STATUS in values:
        status = values[formulas.STATUS]
        if not isinstance(status, str) or status.strip().casefold() not in formulas.STATUS_VALUES:
            raise WorkbookWriteError("invalid_day_status", "day_status must be one of: " + ", ".join(formulas.STATUS_VALUES))
        values[formulas.STATUS] = status.strip().casefold()
    return values


def _guard_daily(data: bytes, sheet_name: str, values: dict) -> None:
    """Daily write rules that depend on the workbook's schema version."""
    if sheet_name != "Daily":
        return
    book = inspect_workbook(data)
    has_status = formulas.daily_has_status(book)
    if formulas.STATUS in values and not has_status:
        raise WorkbookWriteError("schema_outdated", "Daily.day_status needs the schema v2 migration (python -m health_workbook.migrate schema-v2)")
    if has_status and formulas.schema_version(data) >= formulas.SCHEMA_VERSION:
        derived = sorted(set(values).intersection(formulas.DERIVED))
        if derived:
            raise WorkbookWriteError("formula_field", "Formula-owned column(s) cannot be written: " + ", ".join(derived))


def _cell_values(sheet_name: str, values: dict) -> dict:
    keys = TABLE_HEADERS[sheet_name] + ((formulas.STATUS,) if sheet_name == "Daily" else ())
    return {key: values[key] for key in keys if key in values}


def _existing_row(data: bytes, sheet_name: str, values: dict) -> str | None:
    identity = {}
    for key in IDENTITY[sheet_name]:
        if key not in values or values[key] in (None, ""):
            if key in OPTIONAL_IDENTITY.get(sheet_name, ()):
                return None
            raise WorkbookWriteError("missing_field", "Required: " + key)
        identity[key] = values[key]
    return find_row(data, sheet_name, identity)


def _payload_matches(data: bytes, sheet_name: str, row_id: str, values: dict) -> bool:
    current = row_values(data, sheet_name, row_id)
    for key, value in _cell_values(sheet_name, values).items():
        if not _same(current.get(key), value):
            return False
    return True


def _audit(action: str, sheet_name: str, row_id: str | None, values: dict, before, after, reason: str | None = None) -> dict:
    return {
        "action": action,
        "sheet": sheet_name,
        "row_id": row_id,
        "actor": values.get("updated_by") or "migration",
        "source": values.get("source") or "health-workbook",
        "reason": reason,
        "external_id": values.get("external_id"),
        "before_json": None if before is None else json.dumps(before, sort_keys=True, default=str),
        "after_json": json.dumps(after, sort_keys=True, default=str),
    }


def _reject_readonly(sheet_name: str) -> None:
    if sheet_name in READ_ONLY or sheet_name not in WRITABLE:
        raise WorkbookWriteError("writes_disabled", f"{sheet_name} is read-only")


def _validate_dates(sheet_name: str, values: dict) -> None:
    for key, value in values.items():
        if not isinstance(value, str):
            continue
        if key in DATE_EXACT or (sheet_name == "Measurements" and key == "timestamp") or key == "datetime":
            if len(value) < 10 or value[4] != "-" or value[7] != "-":
                raise WorkbookWriteError("invalid_timestamp", f"{key} must start with YYYY-MM-DD")
        if key in DATE_EXACT and len(value) != 10:
            raise WorkbookWriteError("invalid_timestamp", f"{key} must be YYYY-MM-DD")


def _require(record: dict, fields: tuple[str, ...]) -> None:
    if not isinstance(record, dict):
        raise WorkbookWriteError("malformed", "Request body must be an object")
    missing = [field for field in fields if not isinstance(record.get(field), str) or not record[field].strip()]
    if missing:
        raise WorkbookWriteError("missing_field", "Required: " + ", ".join(missing))


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
