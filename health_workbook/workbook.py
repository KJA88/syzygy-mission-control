"""Read an XLSX workbook without changing its bytes."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import sha256
import io
from pathlib import Path, PurePosixPath
import re
import zipfile
from xml.etree import ElementTree

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
EXCEL_EPOCH = datetime(1899, 12, 30)
MAX_BYTES = 25 * 1024 * 1024
MAX_ENTRIES = 5000
MAX_EXPANDED = 64 * 1024 * 1024

REQUIRED_SHEETS = (
    "README",
    "Daily",
    "Meals",
    "Training",
    "Lifts",
    "Notes",
    "Weight Trend",
    "Deficit Bank",
)

TABLE_HEADERS = {
    "Daily": (
        "date", "weight_lb", "body_fat_pct", "sleep_score", "readiness",
        "kcal_in", "protein_g", "carbs_g", "fat_g", "vodka_g", "steps",
        "distance_mi", "active_kcal_fitbit", "azm_total", "hrv_ms", "rhr_bpm",
        "sleep_asleep_min", "fitbit_total_burn", "fitbit_active_burn",
        "fitbit_resting_burn", "fitbit_deficit", "est_maint", "est_deficit",
        "under_1800", "bank_under_1800_cum", "bank_deficit_cum",
        "supplements_ok", "symptom_flag", "weigh_in_notes", "notes", "updated_by",
    ),
    "Meals": (
        "date", "time", "food", "kcal", "protein_g", "carbs_g", "fat_g",
        "notes", "updated_by",
    ),
    "Training": (
        "date", "session_id", "source", "type", "start_local", "duration_min",
        "avg_hr", "max_hr", "calories", "distance_mi", "details", "updated_by",
        "burn_role",
    ),
    "Lifts": (
        "date", "exercise", "reps_set1", "weight_set1", "reps_set2",
        "weight_set2", "notes", "updated_by",
    ),
    "Notes": (
        "datetime", "category", "severity", "suspected_trigger",
        "note_paraphrased", "raw_cue", "updated_by",
    ),
    "Weight Trend": ("date", "weight_lb", "body_fat_pct", "delta_lb", "notes"),
    "Measurements": (
        "timestamp", "metric", "value", "value2", "unit", "source", "device",
        "external_id", "context", "notes", "updated_by", "recorded_at",
    ),
}

PROSE_SHEETS = {"README", "Deficit Bank"}
# Table sheets whose first non-ISO-date row starts a footer (totals/summary block), not data rows.
FOOTER_SHEETS = {"Weight Trend", "Meals"}
DATE_FIELDS = {
    "Daily": "date",
    "Meals": "date",
    "Training": "date",
    "Lifts": "date",
    "Notes": "datetime",
    "Weight Trend": "date",
    "Measurements": "timestamp",
}
SHEET_KEYS = {
    "readme": "README",
    "daily": "Daily",
    "meals": "Meals",
    "training": "Training",
    "lifts": "Lifts",
    "notes": "Notes",
    "weight-trend": "Weight Trend",
    "weighttrend": "Weight Trend",
    "deficit-bank": "Deficit Bank",
    "deficitbank": "Deficit Bank",
    "measurements": "Measurements",
    "measurement": "Measurements",
}
ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class LoadResult:
    present: bool
    valid: bool
    error: str | None = None
    detail: str | None = None
    workbook: Workbook | None = None


@dataclass
class Workbook:
    filename: str
    sha256: str
    sheet_order: list[str]
    sheets: dict[str, Sheet]


@dataclass
class Sheet:
    name: str
    kind: str
    headers: list[str] = field(default_factory=list)
    header_row: int | None = None
    preamble: list[dict] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    footer: list[dict] = field(default_factory=list)


def sheet_key(name: str) -> str:
    return name.strip().casefold().replace("_", "-").replace(" ", "-")


def resolve_sheet_name(value: str) -> str | None:
    return SHEET_KEYS.get(value.strip().casefold().replace("_", "-").replace(" ", "-"))


def inspect_workbook(data: bytes) -> Workbook:
    sheets, order = _parse(data)
    return Workbook("", sha256(data).hexdigest(), order, sheets)


def load_workbook(path: Path) -> LoadResult:
    filename = path.name
    if path.is_symlink():
        return LoadResult(True, False, "workbook_not_regular_file", "Staged workbook must be a regular file")
    if not path.exists():
        return LoadResult(False, False, "workbook_missing", "Staged workbook file is missing")
    if not path.is_file():
        return LoadResult(True, False, "workbook_not_regular_file", "Staged workbook must be a regular file")
    try:
        data = path.read_bytes()
    except OSError as error:
        return LoadResult(True, False, "workbook_unreadable", error.strerror or "unreadable")
    digest = sha256(data).hexdigest()
    try:
        sheets, order = _parse(data)
    except ValueError as error:
        return LoadResult(True, False, "workbook_invalid", str(error), None)
    return LoadResult(
        True,
        True,
        None,
        None,
        Workbook(filename, digest, order, sheets),
    )


def _parse(data: bytes) -> tuple[dict[str, Sheet], list[str]]:
    if len(data) > MAX_BYTES:
        raise ValueError("Workbook exceeds size limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise ValueError("Invalid XLSX workbook") from error
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ENTRIES or sum(item.file_size for item in infos) > MAX_EXPANDED:
            raise ValueError("Workbook exceeds expanded size limit")
        if len({item.filename for item in infos}) != len(infos):
            raise ValueError("Duplicate workbook archive entries")
        names = archive.namelist()
        if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
            raise ValueError("Invalid XLSX workbook")
        if any("vbaproject" in name.lower() for name in names):
            raise ValueError("Macro-enabled workbooks are not accepted")
        if archive.testzip():
            raise ValueError("Workbook archive is corrupt")
        shared = _shared_strings(archive)
        order, targets = _sheet_targets(archive)
        missing = [name for name in REQUIRED_SHEETS if name not in order]
        if missing:
            raise ValueError("Missing required tabs: " + ", ".join(missing))
        parsed: dict[str, Sheet] = {}
        for name in order:
            rows = _read_rows(archive, targets[name], shared)
            if name in PROSE_SHEETS:
                parsed[name] = Sheet(name, "prose", rows=_prose_rows(name, rows))
            elif name in TABLE_HEADERS:
                parsed[name] = _table_sheet(name, rows)
            else:
                parsed[name] = Sheet(name, "unserved", rows=_prose_rows(name, rows))
        return parsed, order


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = _xml(archive.read("xl/sharedStrings.xml"))
    return [
        "".join(node.text or "" for node in item.iter(NS + "t"))
        for item in root.findall(NS + "si")
    ]


def _sheet_targets(archive: zipfile.ZipFile) -> tuple[list[str], dict[str, str]]:
    workbook = _xml(archive.read("xl/workbook.xml"))
    rels = _xml(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        rel.attrib.get("Id"): rel.attrib.get("Target", "")
        for rel in rels.findall(PKG_NS + "Relationship")
    }
    order: list[str] = []
    resolved: dict[str, str] = {}
    for sheet in workbook.findall(NS + "sheets/" + NS + "sheet"):
        name = sheet.attrib.get("name")
        if not name or name in resolved:
            raise ValueError("Workbook has a missing or duplicate sheet name")
        target = targets.get(sheet.attrib.get(REL_NS + "id"), "")
        path = _part_path(target)
        if path not in archive.namelist():
            raise ValueError(f"Missing worksheet part for {name}")
        order.append(name)
        resolved[name] = path
    return order, resolved


def _part_path(target: str) -> str:
    clean = target.lstrip("/")
    path = clean if clean.startswith("xl/") else "xl/" + clean
    if ".." in PurePosixPath(path).parts:
        raise ValueError("Invalid worksheet path")
    return path


def _read_rows(archive: zipfile.ZipFile, part: str, shared: list[str]) -> list[tuple[int, dict[int, object]]]:
    root = _xml(archive.read(part))
    rows: list[tuple[int, dict[int, object]]] = []
    for row in root.iter(NS + "row"):
        number = int(row.attrib.get("r", "0"))
        values: dict[int, object] = {}
        for cell in row.findall(NS + "c"):
            ref = cell.attrib.get("r", "")
            index = _column_index(ref)
            if index is None:
                continue
            value = _cell_value(cell, shared)
            if value is not None:
                values[index] = value
        if values:
            rows.append((number, values))
    return rows


def _table_sheet(name: str, rows: list[tuple[int, dict[int, object]]]) -> Sheet:
    expected = TABLE_HEADERS[name]
    header_at = _header_index(rows, expected)
    if header_at is None:
        raise ValueError(f"{name} header mismatch")
    header_number, header_cells = rows[header_at]
    headers = _header_names(header_cells, expected)
    preamble = [_prose_item(name, number, cells) for number, cells in rows[:header_at]]
    body: list[dict] = []
    footer: list[dict] = []
    seen_gap = False
    for number, cells in rows[header_at + 1:]:
        record = _record(name, number, headers, cells)
        if name in FOOTER_SHEETS and (seen_gap or not _is_iso_day(record.get("date"))):
            seen_gap = True
            footer.append(_prose_item(name, number, cells))
            continue
        body.append(record)
    return Sheet(name, "table", headers, header_number, preamble, body, footer)


def _header_index(rows: list[tuple[int, dict[int, object]]], expected: tuple[str, ...]) -> int | None:
    for index, (_, cells) in enumerate(rows):
        actual = _header_names(cells, expected)
        if actual[: len(expected)] == list(expected):
            return index
    return None


def _header_names(cells: dict[int, object], expected: tuple[str, ...]) -> list[str]:
    last = max([0, *cells.keys(), len(expected)])
    names: list[str] = []
    for column in range(1, last + 1):
        value = cells.get(column)
        names.append("" if value is None else str(value))
    while names and names[-1] == "":
        names.pop()
    return names


def _record(name: str, number: int, headers: list[str], cells: dict[int, object]) -> dict:
    record: dict[str, object] = {
        "row_id": f"{sheet_key(name)}:{number}",
        "row": number,
    }
    for column, header in enumerate(headers, start=1):
        if not header:
            continue
        value = cells.get(column)
        record[header] = _coerce_field(header, value)
    return record


def _coerce_field(header: str, value: object) -> object:
    if value is None:
        return None
    if header in {"date", "datetime"} and isinstance(value, (int, float)) and not isinstance(value, bool):
        if 1 <= float(value) <= 100000:
            stamp = EXCEL_EPOCH + timedelta(days=float(value))
            if header == "datetime":
                return stamp.strftime("%Y-%m-%d %H:%M")
            return stamp.strftime("%Y-%m-%d")
    return value


def _prose_rows(name: str, rows: list[tuple[int, dict[int, object]]]) -> list[dict]:
    return [_prose_item(name, number, cells) for number, cells in rows]


def _prose_item(name: str, number: int, cells: dict[int, object]) -> dict:
    return {
        "row_id": f"{sheet_key(name)}:{number}",
        "row": number,
        "cells": {_column_letter(column): value for column, value in sorted(cells.items())},
    }


def _is_iso_day(value: object) -> bool:
    return isinstance(value, str) and ISO_DAY.fullmatch(value) is not None


def _cell_value(cell: ElementTree.Element, shared: list[str]) -> object | None:
    kind = cell.attrib.get("t", "n")
    if kind == "inlineStr":
        text = "".join(node.text or "" for node in cell.iter(NS + "t"))
        return text
    node = cell.find(NS + "v")
    if node is None or node.text is None:
        return None
    if kind == "s":
        index = int(node.text)
        if index < 0 or index >= len(shared):
            raise ValueError("Shared string index is out of range")
        return shared[index]
    if kind == "b":
        return node.text == "1"
    if kind in {"str", "e"}:
        return node.text
    return _parse_number(node.text)


def _parse_number(text: str) -> object:
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    try:
        value = float(text)
    except ValueError:
        return text
    if value.is_integer() and "e" not in text.lower():
        return int(value)
    return value


def _column_index(ref: str) -> int | None:
    letters = "".join(character for character in ref if character.isalpha())
    if not letters:
        return None
    index = 0
    for character in letters.upper():
        index = index * 26 + ord(character) - 64
    return index


def _column_letter(index: int) -> str:
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _xml(payload: bytes) -> ElementTree.Element:
    folded = payload.upper()
    if b"<!DOCTYPE" in folded or b"<!ENTITY" in folded:
        raise ValueError("Unsupported workbook XML declarations")
    try:
        return ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise ValueError("Invalid XLSX workbook") from error
