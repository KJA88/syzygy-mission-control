"""Serialized Measurements writes: lock, backup, validate, replace, audit."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from health_workbook.mutate import (
    WorkbookWriteError,
    append_measurement,
    find_measurement,
    patch_measurement,
)
from health_workbook.workbook import TABLE_HEADERS, inspect_workbook

MEASUREMENT_FIELDS = set(TABLE_HEADERS["Measurements"])
REQUIRED_APPEND = ("timestamp", "metric", "source", "updated_by", "recorded_at")
REQUIRED_PATCH = ("source", "updated_by", "recorded_at", "reason")


class WriteFault(RuntimeError):
    """Injected failure used by tests."""


class WriteStore:
    def __init__(self, workbook_path: Path, fault: str | None = None):
        self.workbook_path = workbook_path
        self.fault = fault
        self.db_path = workbook_path.parent / "health-workbook-audit.sqlite3"
        self.backup_dir = workbook_path.parent / "backups"

    def append(self, record: dict) -> dict:
        values = _measurement_values(record, REQUIRED_APPEND)
        with self._locked() as connection:
            current = self._read()
            existing = find_measurement(current, values["source"], values.get("external_id"))
            if existing:
                return {"result": "exists", "row_id": existing, "sheet": "Measurements", "backup": None}
            new_bytes, row_id = append_measurement(current, values)
            self._check(new_bytes)
            backup = self._commit(connection, current, new_bytes, {
                "action": "append",
                "row_id": row_id,
                "actor": values["updated_by"],
                "source": values["source"],
                "reason": None,
                "external_id": values.get("external_id"),
                "before_json": None,
                "after_json": json.dumps(values, sort_keys=True),
            })
            return {"result": "appended", "row_id": row_id, "sheet": "Measurements", "backup": backup.name}

    def correct(self, row_id: str, record: dict) -> dict:
        _require(record, REQUIRED_PATCH)
        fields = record.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise WorkbookWriteError("missing_field", "fields must include the corrected values")
        unknown = set(fields).difference(MEASUREMENT_FIELDS)
        if unknown:
            raise WorkbookWriteError("unknown_field", "Unknown measurement field: " + ", ".join(sorted(unknown)))
        values = _measurement_values({"timestamp": "2000-01-01T00:00:00Z", "metric": "placeholder", **fields, **record}, ("source", "updated_by", "recorded_at"))
        # Placeholder keys are only for validation of attribution; real changes are `fields` plus attribution.
        changes = {key: fields[key] for key in fields}
        changes["source"] = record["source"]
        changes["updated_by"] = record["updated_by"]
        changes["recorded_at"] = record["recorded_at"]
        with self._locked() as connection:
            current = self._read()
            external_id = changes.get("external_id")
            if external_id:
                existing = find_measurement(current, changes["source"], str(external_id))
                if existing and existing != row_id:
                    raise WorkbookWriteError("conflict", "external_id already exists for this source")
            new_bytes, before = patch_measurement(current, row_id, changes)
            self._check(new_bytes)
            after = {**before, **changes}
            backup = self._commit(connection, current, new_bytes, {
                "action": "correct",
                "row_id": row_id,
                "actor": record["updated_by"],
                "source": record["source"],
                "reason": record["reason"].strip(),
                "external_id": after.get("external_id"),
                "before_json": json.dumps(before, sort_keys=True),
                "after_json": json.dumps(after, sort_keys=True),
            })
            return {"result": "corrected", "row_id": row_id, "sheet": "Measurements", "backup": backup.name}

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
            self._check(written)
            if self.fault == "audit":
                raise WriteFault("audit")
            connection.execute(
                """INSERT INTO audit(at, action, sheet, row_id, actor, source, reason, external_id, before_json, after_json, backup_name, workbook_sha)
                   VALUES (?, ?, 'Measurements', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _stamp(),
                    audit["action"],
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

    def _check(self, data: bytes) -> None:
        book = inspect_workbook(data)
        sheet = book.sheets.get("Measurements")
        if sheet is None or sheet.headers != list(TABLE_HEADERS["Measurements"]):
            raise WorkbookWriteError("measurements_missing", "Measurements sheet is missing or invalid")

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


def _measurement_values(record: dict, required: tuple[str, ...]) -> dict:
    _require(record, required)
    unknown = set(record).difference(MEASUREMENT_FIELDS | {"fields", "reason"})
    if unknown:
        raise WorkbookWriteError("unknown_field", "Unknown measurement field: " + ", ".join(sorted(unknown)))
    values = {}
    for key in TABLE_HEADERS["Measurements"]:
        if key not in record or record[key] in (None, ""):
            continue
        value = record[key]
        if key in {"value", "value2"} and not isinstance(value, (int, float, str)):
            raise WorkbookWriteError("invalid_value", f"{key} must be text or a number")
        if not isinstance(value, (int, float, str)) or isinstance(value, bool):
            raise WorkbookWriteError("invalid_value", f"{key} must be text or a number")
        values[key] = value.strip() if isinstance(value, str) else value
    timestamp = str(values.get("timestamp", ""))
    if len(timestamp) < 10 or timestamp[4] != "-" or timestamp[7] != "-":
        raise WorkbookWriteError("invalid_timestamp", "timestamp must start with YYYY-MM-DD")
    return values


def _require(record: dict, fields: tuple[str, ...]) -> None:
    if not isinstance(record, dict):
        raise WorkbookWriteError("malformed", "Request body must be an object")
    missing = [field for field in fields if not isinstance(record.get(field), str) or not record[field].strip()]
    if missing:
        raise WorkbookWriteError("missing_field", "Required: " + ", ".join(missing))


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
