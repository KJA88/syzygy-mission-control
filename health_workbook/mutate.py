"""Edit one worksheet without rewriting any other workbook part."""

from __future__ import annotations

from hashlib import sha256
import io
import re
from urllib.parse import unquote
import zipfile

from health_workbook import formulas
from health_workbook.pkg import WorkbookWriteError, package_parts, repack as _repack_parts
from health_workbook.sheetxml import SheetDoc
from health_workbook.workbook import TABLE_HEADERS, inspect_workbook, sheet_key

MEASUREMENTS = "Measurements"
_HEADERS = TABLE_HEADERS[MEASUREMENTS]


def protected_digest(data: bytes, measurements_part: str | None = None) -> dict[str, str]:
    """Hash every package part except the Measurements worksheet."""
    part = measurements_part if measurements_part is not None else _measurements_part(data)
    return {
        name: sha256(payload).hexdigest()
        for name, payload in package_parts(data).items()
        if name != part
    }


def add_measurements_sheet(data: bytes) -> bytes:
    parts = package_parts(data)
    workbook = parts["xl/workbook.xml"].decode("utf-8")
    if 'name="Measurements"' in workbook:
        raise WorkbookWriteError("measurements_exists", "Measurements sheet already exists")
    rels = parts["xl/_rels/workbook.xml.rels"].decode("utf-8")
    content_types = parts["[Content_Types].xml"].decode("utf-8")
    sheet_id, rel_id, part = _next_ids(workbook, rels, list(parts))
    sheet_xml = _empty_sheet_xml().encode("utf-8")
    additions = {
        "xl/workbook.xml": _insert(workbook, "</sheets>", f'<sheet name="Measurements" sheetId="{sheet_id}" state="visible" r:id="{rel_id}" />').encode("utf-8"),
        "xl/_rels/workbook.xml.rels": _insert(
            rels,
            "</Relationships>",
            f'<Relationship Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="/{part}" Id="{rel_id}" />',
        ).encode("utf-8"),
        "[Content_Types].xml": _insert(
            content_types,
            "</Types>",
            f'<Override PartName="/{part}" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml" />',
        ).encode("utf-8"),
        part: sheet_xml,
    }
    rebuilt = _repack(data, additions)
    _assert_existing_parts(data, rebuilt)
    book = inspect_workbook(rebuilt)
    headers = book.sheets[MEASUREMENTS].headers
    if headers != list(_HEADERS):
        raise WorkbookWriteError("measurements_headers", "Measurements header was not written")
    return rebuilt


def append_measurement(data: bytes, values: dict) -> tuple[bytes, str]:
    return append_row(data, MEASUREMENTS, values)


def patch_measurement(data: bytes, row_id: str, values: dict) -> tuple[bytes, dict]:
    return patch_row(data, MEASUREMENTS, row_id, values)


def find_measurement(data: bytes, source: str, external_id: str | None) -> str | None:
    if not external_id:
        return None
    return find_row(data, MEASUREMENTS, {"source": source, "external_id": external_id})


def write_headers(book, sheet_name: str, data: bytes | None = None) -> tuple[str, ...]:
    """Columns this sheet accepts writes for (Daily gains day_status once migrated)."""
    headers = TABLE_HEADERS[sheet_name]
    if sheet_name == "Daily" and formulas.STATUS in book.sheets["Daily"].headers:
        return (*headers, formulas.STATUS)
    return headers


def _daily_formula_owned(book, data: bytes, sheet_name: str) -> bool:
    return sheet_name == "Daily" and formulas.daily_has_status(book) and formulas.schema_version(data) >= formulas.SCHEMA_VERSION


def _cells_for(row_number: int, values: dict, headers: tuple[str, ...], skip: frozenset = frozenset()) -> list[list]:
    cells = []
    for column, name in enumerate(headers, start=1):
        value = values.get(name)
        if name in skip or value is None or value == "":
            continue
        cells.append([column, _cell_xml(column, row_number, value)])
    return cells


def append_row(data: bytes, sheet_name: str, values: dict) -> tuple[bytes, str]:
    part = _require_sheet(data, sheet_name)
    book = inspect_workbook(data)
    headers = write_headers(book, sheet_name)
    if sheet_name == "Meals":
        return _append_meal(data, part, book, values, headers)
    owned = _daily_formula_owned(book, data, sheet_name)
    sheet = package_parts(data)[part].decode("utf-8")
    row_number = _next_row_number(sheet)
    skip = frozenset(formulas.DERIVED) if owned else frozenset()
    row_xml = f'<row r="{row_number}">' + "".join(xml for _, xml in _cells_for(row_number, values, headers, skip)) + "</row>"
    updated = _insert(sheet, "</sheetData>", row_xml)
    rebuilt = _repack(data, {part: updated.encode("utf-8")})
    if sheet_name == "Daily":
        rebuilt = formulas.refresh_caches(rebuilt, ("daily",))
    _assert_protected(data, rebuilt, _allowed_parts(data, sheet_name, part, owned), sheet_name)
    return rebuilt, f"{sheet_key(sheet_name)}:{row_number}"


def _allowed_parts(data: bytes, sheet_name: str, part: str, owned: bool) -> set[str]:
    allowed = {part}
    if owned:
        allowed.add(_sheet_part(data, "Deficit Bank"))
    return allowed


def _append_meal(data: bytes, part: str, book, values: dict, headers: tuple[str, ...]) -> tuple[bytes, str]:
    """Insert a Meals row directly below the last real data row, shifting any footer down."""
    sheet = book.sheets["Meals"]
    insert_at = max([row["row"] for row in sheet.rows], default=sheet.header_row or 1) + 1
    doc = SheetDoc(package_parts(data)[part].decode("utf-8"))
    layout = formulas._meals_layout(book)
    managed = layout is not None and formulas.meals_footer_managed(doc, layout)
    if doc.has_formulas() and not managed:
        raise WorkbookWriteError("meals_formulas_unsupported", "Meals contains formulas this service does not manage; refusing to shift rows")
    if sheet.footer:
        doc.shift_rows(insert_at, 1)
    row = doc.ensure_row(insert_at)
    row.cells = _cells_for(insert_at, values, headers)
    rebuilt = _repack(data, {part: doc.render().encode("utf-8")})
    if managed and values.get("date"):
        book2 = inspect_workbook(rebuilt)
        doc2 = SheetDoc(package_parts(rebuilt)[part].decode("utf-8"))
        if formulas.ensure_daily_totals_row(doc2, book2, str(values["date"])):
            rebuilt = _repack(rebuilt, {part: doc2.render().encode("utf-8")})
    rebuilt = formulas.refresh_caches(rebuilt, ("meals",))
    _assert_protected(data, rebuilt, {part}, "Meals")
    return rebuilt, f"{sheet_key('Meals')}:{insert_at}"


def patch_row(data: bytes, sheet_name: str, row_id: str, values: dict) -> tuple[bytes, dict]:
    row_number = _row_number(sheet_name, row_id)
    part = _require_sheet(data, sheet_name)
    book = inspect_workbook(data)
    headers = write_headers(book, sheet_name)
    current = next((row for row in book.sheets[sheet_name].rows if row["row"] == row_number), None)
    if current is None:
        raise WorkbookWriteError("row_missing", f"{sheet_name} row was not found")
    before = {key: current.get(key) for key in headers}
    merged = dict(before)
    merged.update(values)
    owned = _daily_formula_owned(book, data, sheet_name)
    skip = frozenset(formulas.DERIVED) if owned else frozenset()
    doc = SheetDoc(package_parts(data)[part].decode("utf-8"))
    row = doc.row(row_number)
    if row is None:
        raise WorkbookWriteError("row_missing", f"{sheet_name} row was not found")
    row.cells = _cells_for(row_number, merged, headers, skip)
    rebuilt = _repack(data, {part: doc.render().encode("utf-8")})
    if sheet_name in ("Daily", "Meals"):
        rebuilt = formulas.refresh_caches(rebuilt, ("daily",) if sheet_name == "Daily" else ("meals",))
    _assert_protected(data, rebuilt, _allowed_parts(data, sheet_name, part, owned), sheet_name)
    return rebuilt, before


def find_row(data: bytes, sheet_name: str, identity: dict) -> str | None:
    book = inspect_workbook(data)
    sheet = book.sheets.get(sheet_name)
    if sheet is None:
        code = "measurements_missing" if sheet_name == MEASUREMENTS else "sheet_missing"
        raise WorkbookWriteError(code, f"{sheet_name} sheet is missing")
    for row in sheet.rows:
        if all(_same(row.get(key), identity[key]) for key in identity):
            return row["row_id"]
    return None


def row_values(data: bytes, sheet_name: str, row_id: str) -> dict:
    row_number = _row_number(sheet_name, row_id)
    book = inspect_workbook(data)
    current = next((row for row in book.sheets[sheet_name].rows if row["row"] == row_number), None)
    if current is None:
        raise WorkbookWriteError("row_missing", f"{sheet_name} row was not found")
    return {key: current.get(key) for key in write_headers(book, sheet_name)}


def _require_measurements(data: bytes) -> str:
    return _require_sheet(data, MEASUREMENTS)


def _require_sheet(data: bytes, sheet_name: str) -> str:
    try:
        return _sheet_part(data, sheet_name)
    except WorkbookWriteError:
        raise
    except ValueError as error:
        raise WorkbookWriteError("workbook_invalid", str(error)) from error


def _measurements_part(data: bytes) -> str:
    return _sheet_part(data, MEASUREMENTS)


def _sheet_part(data: bytes, sheet_name: str) -> str:
    from health_workbook.pkg import sheet_part
    return sheet_part(data, sheet_name)


def _assert_existing_parts(before: bytes, after: bytes) -> None:
    allowed = {"xl/workbook.xml", "xl/_rels/workbook.xml.rels", "[Content_Types].xml"}
    old = package_parts(before)
    new = package_parts(after)
    for name, payload in old.items():
        if name in allowed:
            continue
        if new.get(name) != payload:
            raise WorkbookWriteError("protected_sheet_changed", "A sheet other than Measurements changed")


def _assert_protected(before: bytes, after: bytes, sheet_part, sheet_name: str) -> None:
    parts = {sheet_part} if isinstance(sheet_part, str) else set(sheet_part)
    old, new = package_parts(before), package_parts(after)
    for name in set(old) | set(new):
        if name in parts:
            continue
        if old.get(name) != new.get(name):
            raise WorkbookWriteError("protected_sheet_changed", f"A sheet other than {sheet_name} changed")
    inspect_workbook(after)


def _repack(data: bytes, replacements: dict[str, bytes]) -> bytes:
    return _repack_parts(data, replacements)


def _insert(text: str, marker: str, insertion: str) -> str:
    index = text.rfind(marker)
    if index < 0:
        raise WorkbookWriteError("workbook_invalid", f"Workbook is missing {marker}")
    return text[:index] + insertion + text[index:]


def _next_ids(workbook: str, rels: str, names: list[str]) -> tuple[int, str, str]:
    sheet_ids = [int(value) for value in re.findall(r'sheetId="(\d+)"', workbook)]
    rel_ids = [int(value) for value in re.findall(r'Id="rId(\d+)"', rels)]
    sheet_numbers = [int(value) for value in re.findall(r"worksheets/sheet(\d+)\.xml", rels + " " + " ".join(names))]
    part = f"xl/worksheets/sheet{max(sheet_numbers, default=0) + 1}.xml"
    return max(sheet_ids, default=0) + 1, f"rId{max(rel_ids, default=0) + 1}", part


def _empty_sheet_xml() -> str:
    header = {name: name for name in _HEADERS}
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{_row_xml(1, header)}</sheetData></worksheet>"
    )


def _row_xml(row_number: int, values: dict, headers: tuple[str, ...] | None = None) -> str:
    columns = headers or _HEADERS
    cells = []
    for column, name in enumerate(columns, start=1):
        value = values.get(name)
        if value is None or value == "":
            continue
        cells.append(_cell_xml(column, row_number, value))
    return f'<row r="{row_number}">{"".join(cells)}</row>'


def _cell_xml(column: int, row_number: int, value: object) -> str:
    ref = f"{_column_letter(column)}{row_number}"
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise WorkbookWriteError("invalid_value", "Measurement values must be text or numbers")
    if isinstance(value, (int, float)):
        number = int(value) if float(value).is_integer() else value
        return f'<c r="{ref}"><v>{number}</v></c>'
    text = str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'


def _column_letter(index: int) -> str:
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _next_row_number(sheet_xml: str) -> int:
    numbers = [int(value) for value in re.findall(r'<row r="(\d+)"', sheet_xml)]
    return max(numbers, default=1) + 1


def canonical_row_id(sheet_name: str, row_id: str) -> str:
    return f"{sheet_key(sheet_name)}:{_row_number(sheet_name, row_id)}"


def _row_number(sheet_name: str, row_id: str) -> int:
    """Public row ids are ``{sheet}:{excel row}``. Also accept that id percent-encoded, a different prefix case, or the bare Excel row number."""
    token = unquote(str(row_id or "")).strip()
    key = sheet_key(sheet_name)
    prefixed = re.fullmatch(r"([a-z0-9-]+):(\d+)", token, flags=re.IGNORECASE)
    if prefixed:
        if prefixed.group(1).casefold() != key:
            raise WorkbookWriteError("row_missing", f"{sheet_name} row was not found")
        number = int(prefixed.group(2))
    elif re.fullmatch(r"\d+", token):
        number = int(token)
    else:
        raise WorkbookWriteError("row_missing", f"{sheet_name} row was not found")
    if number <= 1:
        raise WorkbookWriteError("row_missing", f"{sheet_name} row was not found")
    return number


def _same(left: object, right: object) -> bool:
    if left == right:
        return True
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    return False

