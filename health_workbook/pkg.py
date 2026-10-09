"""Package-level helpers shared by mutate.py and formulas.py (no workbook semantics)."""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree


class WorkbookWriteError(ValueError):
    def __init__(self, code: str, detail: str, row_id: str | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.row_id = row_id


def package_parts(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {info.filename: archive.read(info.filename) for info in archive.infolist()}


def repack(data: bytes, replacements: dict[str, bytes]) -> bytes:
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


def sheet_part(data: bytes, sheet_name: str) -> str:
    """Return the worksheet part name for sheet_name."""
    from health_workbook.workbook import NS, PKG_NS, REL_NS, _part_path

    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        workbook = archive.read("xl/workbook.xml")
        rels = archive.read("xl/_rels/workbook.xml.rels")
    root = ElementTree.fromstring(workbook)
    rel_root = ElementTree.fromstring(rels)
    targets = {
        rel.attrib.get("Id"): rel.attrib.get("Target", "")
        for rel in rel_root.findall(PKG_NS + "Relationship")
    }
    for sheet in root.findall(NS + "sheets/" + NS + "sheet"):
        if sheet.attrib.get("name") != sheet_name:
            continue
        return _part_path(targets.get(sheet.attrib.get(REL_NS + "id"), ""))
    code = "measurements_missing" if sheet_name == "Measurements" else "sheet_missing"
    raise WorkbookWriteError(code, f"{sheet_name} sheet is missing")


def xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def col_letter(index: int) -> str:
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def col_index(letters: str) -> int:
    index = 0
    for character in letters.upper():
        index = index * 26 + ord(character) - 64
    return index


CELL_REF = re.compile(r"([A-Z]+)(\d+)")
