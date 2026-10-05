"""Minimal row/cell editor for a worksheet's <sheetData>, preserving untouched cell XML verbatim."""

from __future__ import annotations

import re

from health_workbook.pkg import CELL_REF, WorkbookWriteError, col_index, col_letter

_ROW = re.compile(r"<row\b([^>]*?)(/>|>(.*?)</row>)", re.DOTALL)
_CELL = re.compile(r"<c\b([^>]*?)(/>|>(.*?)</c>)", re.DOTALL)
_ROWNUM = re.compile(r'\br="(\d+)"')
_CELLREF = re.compile(r'(<c\b[^>]*?\br=")([A-Z]+)(\d+)(")')
_REF_ATTRS = re.compile(r'\b(ref|sqref)="([^"]*)"')


class Row:
    def __init__(self, number: int, attrs: str, cells: list[list], selfclosed: bool = False):
        self.number = number
        self.attrs = attrs
        self.cells = cells  # [[col_index, xml], ...] in column order
        self.selfclosed = selfclosed

    def get(self, col: int) -> str | None:
        for index, xml in self.cells:
            if index == col:
                return xml
        return None

    def set(self, col: int, xml: str) -> None:
        for item in self.cells:
            if item[0] == col:
                item[1] = xml
                return
        self.cells.append([col, xml])
        self.cells.sort(key=lambda item: item[0])
        self.selfclosed = False

    def drop(self, col: int) -> None:
        self.cells = [item for item in self.cells if item[0] != col]

    def render(self) -> str:
        if not self.cells and self.selfclosed:
            return f'<row r="{self.number}"{self.attrs}/>'
        return f'<row r="{self.number}"{self.attrs}>' + "".join(xml for _, xml in self.cells) + "</row>"


class SheetDoc:
    def __init__(self, xml: str):
        match = re.search(r"<sheetData\s*/>", xml)
        if match:
            xml = xml[: match.start()] + "<sheetData></sheetData>" + xml[match.end():]
        start = xml.find("<sheetData>")
        end = xml.rfind("</sheetData>")
        if start < 0 or end < 0:
            raise WorkbookWriteError("workbook_invalid", "Worksheet is missing sheetData")
        self.head = xml[: start + len("<sheetData>")]
        self.tail = xml[end:]
        body = xml[start + len("<sheetData>"): end]
        self.rows: list[Row] = []
        for m in _ROW.finditer(body):
            attrs = m.group(1)
            number = int(_ROWNUM.search(attrs).group(1))
            attrs = _ROWNUM.sub("", attrs, count=1)
            attrs = re.sub(r"\s+", " ", attrs.rstrip("/")).rstrip()
            if attrs.strip() == "":
                attrs = ""
            elif not attrs.startswith(" "):
                attrs = " " + attrs
            inner = m.group(3) or ""
            cells = []
            for c in _CELL.finditer(inner):
                ref = CELL_REF.search(re.search(r'\br="([^"]+)"', c.group(1)).group(1))
                cells.append([col_index(ref.group(1)), c.group(0)])
            self.rows.append(Row(number, attrs, cells, m.group(2) == "/>"))

    def row(self, number: int) -> Row | None:
        for row in self.rows:
            if row.number == number:
                return row
        return None

    def ensure_row(self, number: int) -> Row:
        row = self.row(number)
        if row is None:
            row = Row(number, "", [])
            self.rows.append(row)
            self.rows.sort(key=lambda item: item.number)
        return row

    def shift_rows(self, at: int, by: int) -> None:
        """Renumber every row >= at (and its cell refs) by +by, like Excel's insert row."""
        for row in self.rows:
            if row.number < at:
                continue
            row.number += by
            for item in row.cells:
                item[1] = _CELLREF.sub(lambda m: f"{m.group(1)}{m.group(2)}{row.number}{m.group(4)}", item[1], count=1)
        self.rows.sort(key=lambda item: item.number)
        self.head = self._shift_dimension(self.head, at, by)
        self.tail = _shift_ref_attrs(self.tail, at, by)

    @staticmethod
    def _shift_dimension(head: str, at: int, by: int) -> str:
        return head  # dimension is recomputed in render()

    def render(self) -> str:
        head = self.head
        if self.rows:
            max_row = max(row.number for row in self.rows)
            max_col = max([item[0] for row in self.rows for item in row.cells] or [1])
            head = re.sub(r'<dimension ref="[^"]*"\s*/>', f'<dimension ref="A1:{col_letter(max_col)}{max_row}" />', head, count=1)
        return head + "".join(row.render() for row in self.rows) + self.tail

    def has_formulas(self) -> bool:
        return any("<f" in xml for row in self.rows for _, xml in row.cells)


def _shift_cell_ref(ref: str, at: int, by: int) -> str:
    def one(m):
        row = int(m.group(2))
        return m.group(1) + str(row + by if row >= at else row)
    return re.sub(r"([A-Z]+)(\d+)", one, ref)


def _shift_ref_attrs(text: str, at: int, by: int) -> str:
    """Shift ref/sqref attributes (mergeCells, conditional formats, validations) after insert."""
    return _REF_ATTRS.sub(lambda m: f'{m.group(1)}="' + " ".join(_shift_cell_ref(part, at, by) for part in m.group(2).split(" ")) + '"', text)


def inline_text_cell(ref: str, text: str, style: str | None = None) -> str:
    from health_workbook.pkg import xml_escape
    s = f' s="{style}"' if style else ""
    return f'<c r="{ref}"{s} t="inlineStr"><is><t>{xml_escape(text)}</t></is></c>'


def number_text(value) -> str:
    if isinstance(value, float):
        value = round(value, 6)
        if value.is_integer():
            return str(int(value))
        return repr(value)
    return str(value)


def formula_cell(ref: str, formula: str, value, style: str | None = None) -> str:
    """A real formula cell with a cached result (numbers or empty text)."""
    from health_workbook.pkg import xml_escape
    s = f' s="{style}"' if style else ""
    text = formula[1:] if formula.startswith("=") else formula
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}"{s}><f>{xml_escape(text)}</f><v>{number_text(value)}</v></c>'
    return f'<c r="{ref}"{s} t="str"><f>{xml_escape(text)}</f><v></v></c>'


def cell_style(xml: str | None) -> str | None:
    if not xml:
        return None
    m = re.match(r'<c\b[^>]*?\bs="(\d+)"', xml)
    return m.group(1) if m else None
