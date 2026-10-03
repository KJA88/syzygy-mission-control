"""Edit one worksheet without rewriting any other workbook part."""

from __future__ import annotations

from hashlib import sha256
import io
import re
import zipfile

from health_workbook.workbook import TABLE_HEADERS, inspect_workbook, sheet_key

MEASUREMENTS = "Measurements"
_HEADERS = TABLE_HEADERS[MEASUREMENTS]


class WorkbookWriteError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def package_parts(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {info.filename: archive.read(info.filename) for info in archive.infolist()}


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


def append_row(data: bytes, sheet_name: str, values: dict) -> tuple[bytes, str]:
    part = _require_sheet(data, sheet_name)
    headers = TABLE_HEADERS[sheet_name]
    sheet = package_parts(data)[part].decode("utf-8")
    row_number = _next_row_number(sheet)
    updated = _insert(sheet, "</sheetData>", _row_xml(row_number, values, headers))
    rebuilt = _repack(data, {part: updated.encode("utf-8")})
    _assert_protected(data, rebuilt, part, sheet_name)
    return rebuilt, f"{sheet_key(sheet_name)}:{row_number}"


def patch_row(data: bytes, sheet_name: str, row_id: str, values: dict) -> tuple[bytes, dict]:
    row_number = _row_number(sheet_name, row_id)
    part = _require_sheet(data, sheet_name)
    headers = TABLE_HEADERS[sheet_name]
    book = inspect_workbook(data)
    current = next((row for row in book.sheets[sheet_name].rows if row["row"] == row_number), None)
    if current is None:
        raise WorkbookWriteError("row_missing", f"{sheet_name} row was not found")
    before = {key: current.get(key) for key in headers}
    merged = dict(before)
    merged.update(values)
    sheet = package_parts(data)[part].decode("utf-8")
    pattern = re.compile(rf'<row r="{row_number}"[^>]*>.*?</row>', re.DOTALL)
    updated, count = pattern.subn(_row_xml(row_number, merged, headers), sheet, count=1)
    if count != 1:
        raise WorkbookWriteError("row_missing", f"{sheet_name} row was not found")
    rebuilt = _repack(data, {part: updated.encode("utf-8")})
    _assert_protected(data, rebuilt, part, sheet_name)
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
    return {key: current.get(key) for key in TABLE_HEADERS[sheet_name]}


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
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        workbook = archive.read("xl/workbook.xml")
        rels = archive.read("xl/_rels/workbook.xml.rels")
    from xml.etree import ElementTree
    from health_workbook.workbook import NS, PKG_NS, REL_NS, _part_path

    root = ElementTree.fromstring(workbook)
    rel_root = ElementTree.fromstring(rels)
    targets = {
        rel.attrib.get("Id"): rel.attrib.get("Target", "")
        for rel in rel_root.findall(PKG_NS + "Relationship")
    }
    for sheet in root.findall(NS + "sheets/" + NS + "sheet"):
        if sheet.attrib.get("name") != sheet_name:
            continue
        part = _part_path(targets.get(sheet.attrib.get(REL_NS + "id"), ""))
        return part
    code = "measurements_missing" if sheet_name == MEASUREMENTS else "sheet_missing"
    raise WorkbookWriteError(code, f"{sheet_name} sheet is missing")


def _assert_existing_parts(before: bytes, after: bytes) -> None:
    allowed = {"xl/workbook.xml", "xl/_rels/workbook.xml.rels", "[Content_Types].xml"}
    old = package_parts(before)
    new = package_parts(after)
    for name, payload in old.items():
        if name in allowed:
            continue
        if new.get(name) != payload:
            raise WorkbookWriteError("protected_sheet_changed", "A sheet other than Measurements changed")


def _assert_protected(before: bytes, after: bytes, sheet_part: str, sheet_name: str) -> None:
    if protected_digest(before, sheet_part) != protected_digest(after, sheet_part):
        raise WorkbookWriteError("protected_sheet_changed", f"A sheet other than {sheet_name} changed")
    inspect_workbook(after)


def _repack(data: bytes, replacements: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(buffer, "w") as output:
        seen = set()
        for info in source.infolist():
            payload = replacements.get(info.filename, source.read(info.filename))
            if info.filename in replacements:
                output.writestr(info.filename, payload, compress_type=zipfile.ZIP_DEFLATED)
            else:
                output.writestr(info, payload)
            seen.add(info.filename)
        for name, payload in replacements.items():
            if name not in seen:
                output.writestr(name, payload, compress_type=zipfile.ZIP_DEFLATED)
    return buffer.getvalue()


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


def _row_number(sheet_name: str, row_id: str) -> int:
    match = re.fullmatch(re.escape(sheet_key(sheet_name)) + r":(\d+)", row_id or "")
    if match is None or int(match.group(1)) <= 1:
        raise WorkbookWriteError("row_missing", f"{sheet_name} row was not found")
    return int(match.group(1))


def _same(left: object, right: object) -> bool:
    if left == right:
        return True
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return float(left) == float(right)
    return False

