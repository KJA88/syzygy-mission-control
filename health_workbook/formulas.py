"""Schema v2: Daily.day_status, formula-driven deficit/under-1800 logic, Meals footer formulas.

Everything here is deterministic: a formula's text depends only on its row number and the sheet's
header positions, and its cached <v> value is recomputed from the sheet's stored values after every
service write (refresh_caches).  Excel/LibreOffice recalculate on load (workbook has fullCalcOnLoad=1),
so the cached values only matter to this service's own readers.
"""

from __future__ import annotations

import re

from health_workbook.pkg import WorkbookWriteError, col_letter, package_parts, repack, sheet_part, xml_escape
from health_workbook.sheetxml import SheetDoc, cell_style, formula_cell, inline_text_cell
from health_workbook.workbook import TABLE_HEADERS, inspect_workbook

STATUS = "day_status"
STATUS_VALUES = ("complete", "open")
DERIVED = (
    "fitbit_deficit", "est_maint", "est_deficit", "under_1800",
    "bank_under_1800_cum", "bank_deficit_cum",
)
DAILY_LAST_ROW = 1000
SCHEMA_NAME = "hw_schema_version"
SCHEMA_VERSION = 2
PLAN_CEILING = 1800
MEAL_VALUE_COLUMNS = (("D", "kcal"), ("E", "protein_g"), ("F", "carbs_g"), ("G", "fat_g"))


# ---------------------------------------------------------------- schema detection

def schema_version(data: bytes) -> int:
    parts = package_parts(data)
    workbook = parts["xl/workbook.xml"].decode("utf-8")
    match = re.search(rf'<definedName\b[^>]*\bname="{SCHEMA_NAME}"[^>]*>\s*(\d+)\s*</definedName>', workbook)
    return int(match.group(1)) if match else 1


def daily_has_status(book) -> bool:
    return STATUS in book.sheets["Daily"].headers


def is_v2(data: bytes) -> bool:
    return daily_has_status(inspect_workbook(data)) and schema_version(data) >= SCHEMA_VERSION


# ---------------------------------------------------------------- Daily formulas

def _letters(headers: list[str]) -> dict[str, str]:
    return {name: col_letter(index) for index, name in enumerate(headers, start=1) if name}


def daily_formulas(row: int, L: dict[str, str]) -> dict[str, str]:
    s, f, b = f"${L[STATUS]}{row}", f"${L['kcal_in']}{row}", f"${L['fitbit_total_burn']}{row}"
    a = L["date"]
    last = DAILY_LAST_ROW

    def rng(name: str) -> str:
        return f"${L[name]}$2:${L[name]}${last}"

    def cumulative(source: str) -> str:
        cell = f"${L[source]}{row}"
        return (
            f'=IF({cell}="","",SUMPRODUCT(--({rng("date")}<=${a}{row}),'
            f'--({rng(STATUS)}="complete"),{rng(source)}))'
        )

    ded, maint, est, under = (f"${L[n]}{row}" for n in ("fitbit_deficit", "est_maint", "est_deficit", "under_1800"))
    return {
        "fitbit_deficit": f'=IF(AND({s}="complete",ISNUMBER({b}),ISNUMBER({f})),{b}-{f},"")',
        "est_maint": f'=IF(AND({s}="complete",ISNUMBER({b})),{b},"")',
        "est_deficit": f'=IF(AND(ISNUMBER({maint}),ISNUMBER({f})),{maint}-{f},"")',
        "under_1800": f'=IF(AND({s}="complete",ISNUMBER({f})),{PLAN_CEILING}-{f},"")',
        "bank_under_1800_cum": cumulative("under_1800"),
        "bank_deficit_cum": cumulative("fitbit_deficit"),
    }


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def compute_daily(rows: list[dict]) -> dict[int, dict[str, object]]:
    """Python mirror of the Daily formulas. rows: inspected Daily rows. Returns row -> derived values."""
    out: dict[int, dict[str, object]] = {}
    scoped = [r for r in rows if 2 <= r["row"] <= DAILY_LAST_ROW]
    for r in scoped:
        complete = str(r.get(STATUS) or "").casefold() == "complete"
        kcal, burn = _num(r.get("kcal_in")), _num(r.get("fitbit_total_burn"))
        values: dict[str, object] = {name: "" for name in DERIVED}
        if complete and burn is not None and kcal is not None:
            values["fitbit_deficit"] = burn - kcal
        if complete and burn is not None:
            values["est_maint"] = burn
        if values["est_maint"] != "" and kcal is not None:
            values["est_deficit"] = values["est_maint"] - kcal
        if complete and kcal is not None:
            values["under_1800"] = PLAN_CEILING - kcal
        out[r["row"]] = values
    for r in scoped:
        for cum, source in (("bank_under_1800_cum", "under_1800"), ("bank_deficit_cum", "fitbit_deficit")):
            if out[r["row"]][source] == "":
                continue
            day = _text(r.get("date"))
            total = 0
            for q in scoped:
                if str(q.get(STATUS) or "").casefold() != "complete":
                    continue
                if _text(q.get("date")) <= day and out[q["row"]][source] != "":
                    total += out[q["row"]][source]
            out[r["row"]][cum] = total
    return out


def _text(value) -> str:
    return "" if value is None else str(value)


# ---------------------------------------------------------------- Deficit Bank sheet

DB_TEXT = {
    7: "COMPLETED days only (Daily.day_status = complete). Live formulas over the Daily tab; open or unlabeled days are excluded.",
    8: "Completed days counted",
    9: "Intake (kcal)",
    10: "Fitbit total burn (kcal)",
    11: "Fitbit-based deficit (kcal) — tracker estimate, optimistic/unverified",
    12: "Under-1800 bank (kcal) — room under the 1800 plan ceiling; negative = over",
    13: "Days present in Daily but NOT counted (open or no day_status)",
    21: "Completed-days Fitbit-deficit cumulative (kcal)",
    22: "Completed-days under-1800 bank cumulative (kcal)",
}
DB_HEADER_CELLS = {"B7": "total", "C7": "avg/day", "D7": "days"}


def deficit_bank_cells(L: dict[str, str], daily: list[dict], calc: dict[int, dict]) -> dict[str, tuple[str, object]]:
    last = DAILY_LAST_ROW

    def rng(name: str) -> str:
        return f"Daily!${L[name]}$2:${L[name]}${last}"

    status = rng(STATUS)
    scoped = [r for r in daily if 2 <= r["row"] <= last]
    complete = [r for r in scoped if str(r.get(STATUS) or "").casefold() == "complete"]

    def raw_total(name: str):
        values = [_num(r.get(name)) for r in complete]
        return sum(v for v in values if v is not None), sum(1 for v in values if v is not None)

    def derived_total(name: str):
        values = [calc[r["row"]][name] for r in scoped if calc[r["row"]][name] != ""]
        return sum(values), len(values)

    cells: dict[str, tuple[str, object]] = {}
    cells["B8"] = (f'=COUNTIF({status},"complete")', len(complete))
    for row_no, name, kind in ((9, "kcal_in", "raw"), (10, "fitbit_total_burn", "raw"), (11, "fitbit_deficit", "derived"), (12, "under_1800", "derived")):
        total, count = raw_total(name) if kind == "raw" else derived_total(name)
        if kind == "raw":
            cells[f"B{row_no}"] = (f'=SUMPRODUCT(--({status}="complete"),{rng(name)})', total)
            cells[f"D{row_no}"] = (f'=SUMPRODUCT(--({status}="complete"),--ISNUMBER({rng(name)}))', count)
        else:
            cells[f"B{row_no}"] = (f"=SUM({rng(name)})", total)
            cells[f"D{row_no}"] = (f"=COUNT({rng(name)})", count)
        cells[f"C{row_no}"] = (f'=IF($D{row_no}=0,"",$B{row_no}/$D{row_no})', (total / count) if count else "")
    present = sum(1 for r in scoped if _text(r.get("date")) != "")
    cells["B13"] = (f'=SUMPRODUCT(--({rng("date")}<>""))-$B$8', present - len(complete))
    cells["B21"] = ("=$B$11", cells["B11"][1])
    cells["B22"] = ("=$B$12", cells["B12"][1])
    return cells


def _write_deficit_bank(doc: SheetDoc, cells: dict[str, tuple[str, object]], with_text: bool) -> None:
    if with_text:
        for row_no, text in DB_TEXT.items():
            row = doc.ensure_row(row_no)
            keep = row.get(1)
            style = cell_style(keep)
            row.cells = []
            row.set(1, inline_text_cell(f"A{row_no}", text, style))
        for ref, text in DB_HEADER_CELLS.items():
            row = doc.ensure_row(int(ref[1:]))
            row.set(ord(ref[0]) - 64, inline_text_cell(ref, text))
    for ref, (formula, value) in cells.items():
        row = doc.ensure_row(int(ref[1:]))
        row.set(ord(ref[0]) - 64, formula_cell(ref, formula, value))


# ---------------------------------------------------------------- Meals footer (TOTAL + DAILY TOTALS)

def _meals_layout(book) -> dict | None:
    sheet = book.sheets["Meals"]
    footer = sheet.footer
    if not footer:
        return None
    total = next((f for f in footer if f["cells"].get("A") == "TOTAL"), None)
    daily_head = next((f for f in footer if f["cells"].get("A") == "DAILY TOTALS"), None)
    day_rows = []
    if daily_head is not None:
        after = [f for f in footer if f["row"] > daily_head["row"]]
        header = next((f for f in after if f["cells"].get("A") == "date"), None)
        if header is not None:
            day_rows = [f for f in after if f["row"] > header["row"] and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(f["cells"].get("A", "")))]
    return {"total": total, "day_rows": day_rows, "first_footer": footer[0]["row"], "header_row": sheet.header_row}


def meals_footer_managed(doc: SheetDoc, layout: dict) -> bool:
    total = layout["total"]
    if total is None:
        return False
    row = doc.row(total["row"])
    cell = row.get(4) if row else None
    return bool(cell and "<f>" in cell)


def _meal_range_end(layout: dict) -> int:
    return (layout["total"]["row"] if layout["total"] else layout["first_footer"]) - 1


def meals_formulas(layout: dict, book) -> dict[str, tuple[str, object]]:
    sheet = book.sheets["Meals"]
    end = _meal_range_end(layout)
    first = layout["header_row"] + 1
    rows = [r for r in sheet.rows if first <= r["row"] <= end]
    cells: dict[str, tuple[str, object]] = {}
    if layout["total"]:
        t = layout["total"]["row"]
        for letter, name in MEAL_VALUE_COLUMNS:
            cells[f"{letter}{t}"] = (f"=SUM({letter}${first}:{letter}{end})", sum(_num(r.get(name)) or 0 for r in rows))
    for item in layout["day_rows"]:
        r = item["row"]
        label = item["cells"]["A"]
        for target, (letter, name) in zip("BCDE", MEAL_VALUE_COLUMNS):
            cells[f"{target}{r}"] = (
                f"=SUMPRODUCT(--($A${first}:$A${end}=$A{r}),${letter}${first}:${letter}${end})",
                sum(_num(x.get(name)) or 0 for x in rows if x.get("date") == label),
            )
    return cells


def _apply_cells(doc: SheetDoc, cells: dict[str, tuple[str, object]]) -> None:
    for ref, (formula, value) in cells.items():
        m = re.fullmatch(r"([A-Z]+)(\d+)", ref)
        from health_workbook.pkg import col_index
        col, number = col_index(m.group(1)), int(m.group(2))
        row = doc.ensure_row(number)
        style = cell_style(row.get(col))
        row.set(col, formula_cell(ref, formula, value, style))


def ensure_daily_totals_row(doc: SheetDoc, book, day: str) -> bool:
    """Add a DAILY TOTALS line for a new meal date (managed footers only). Returns True if added."""
    layout = _meals_layout(book)
    if layout is None or not meals_footer_managed(doc, layout) or not layout["day_rows"]:
        return False
    if any(item["cells"]["A"] == day for item in layout["day_rows"]):
        return False
    last = max(item["row"] for item in layout["day_rows"])
    row = doc.ensure_row(last + 1)
    row.set(1, inline_text_cell(f"A{last + 1}", day))
    return True


# ---------------------------------------------------------------- cache refresh

def _put(replacements: dict, parts: dict, name: str, rendered: str) -> None:
    payload = rendered.encode("utf-8")
    if payload != parts[name]:
        replacements[name] = payload


def refresh_caches(data: bytes, scopes: tuple[str, ...] = ("daily", "meals")) -> bytes:
    """Recompute managed formula cells (formula text + cached value) from stored inputs.

    scopes limits what may be rewritten, so a Training/Notes/Measurements write never touches Daily/Meals."""
    book = inspect_workbook(data)
    replacements: dict[str, bytes] = {}
    parts = package_parts(data)
    if "daily" in scopes and daily_has_status(book):
        daily_sheet = book.sheets["Daily"]
        L = _letters(daily_sheet.headers)
        part = sheet_part(data, "Daily")
        doc = SheetDoc(parts[part].decode("utf-8"))
        calc = compute_daily(daily_sheet.rows)
        for r in daily_sheet.rows:
            if _text(r.get("date")) == "" or r["row"] > DAILY_LAST_ROW:
                continue
            formulas = daily_formulas(r["row"], L)
            row = doc.ensure_row(r["row"])
            for name in DERIVED:
                col = daily_sheet.headers.index(name) + 1
                style = cell_style(row.get(col))
                ref = f"{col_letter(col)}{r['row']}"
                row.set(col, formula_cell(ref, formulas[name], calc[r["row"]][name], style))
        _put(replacements, parts, part, doc.render())
        if "Deficit Bank" in book.sheets and schema_version(data) >= SCHEMA_VERSION:
            db_part = sheet_part(data, "Deficit Bank")
            db_doc = SheetDoc(parts[db_part].decode("utf-8"))
            if any("<f>" in x for row in db_doc.rows for _, x in row.cells):
                _write_deficit_bank(db_doc, deficit_bank_cells(L, daily_sheet.rows, calc), with_text=False)
                _put(replacements, parts, db_part, db_doc.render())
    layout = _meals_layout(book) if "meals" in scopes else None
    if layout is not None:
        part = sheet_part(data, "Meals")
        doc = SheetDoc(parts[part].decode("utf-8"))
        if meals_footer_managed(doc, layout):
            _apply_cells(doc, meals_formulas(layout, book))
            _put(replacements, parts, part, doc.render())
    return repack(data, replacements) if replacements else data


# ---------------------------------------------------------------- migration

def migrate_v2(data: bytes) -> bytes:
    """Schema v1 -> v2. Idempotent: returns data unchanged when already v2."""
    book = inspect_workbook(data)
    daily = book.sheets["Daily"]
    present = daily_has_status(book)
    if present and schema_version(data) >= SCHEMA_VERSION:
        return data
    base = list(TABLE_HEADERS["Daily"])
    if daily.headers[: len(base)] != base or (present and daily.headers != base + [STATUS]) or (not present and daily.headers != base):
        raise WorkbookWriteError("daily_headers", "Daily header does not match the expected v1/v2 layout")
    parts = package_parts(data)
    replacements: dict[str, bytes] = {}

    # Daily: add the day_status header (new last column; no existing column moves).
    daily_part = sheet_part(data, "Daily")
    doc = SheetDoc(parts[daily_part].decode("utf-8"))
    status_col = len(base) + 1
    header = doc.row(daily.header_row)
    if header is None:
        raise WorkbookWriteError("daily_headers", "Daily header row not found")
    if not present:
        header.set(status_col, inline_text_cell(f"{col_letter(status_col)}{daily.header_row}", STATUS, cell_style(header.get(status_col - 1))))
    replacements[daily_part] = doc.render().encode("utf-8")

    # Deficit Bank: replace the stale static summary lines with formula cells (values filled by refresh).
    db_part = sheet_part(data, "Deficit Bank")
    db_doc = SheetDoc(parts[db_part].decode("utf-8"))
    L = _letters(base + [STATUS])
    placeholder = {}
    for ref in ("B8", "B9", "C9", "D9", "B10", "C10", "D10", "B11", "C11", "D11", "B12", "C12", "D12", "B13", "B21", "B22"):
        placeholder[ref] = ("=0", 0)
    _write_deficit_bank(db_doc, placeholder, with_text=True)
    replacements[db_part] = db_doc.render().encode("utf-8")

    # Meals footer: TOTAL + DAILY TOTALS become formulas (managed by refresh_caches).
    layout = _meals_layout(book)
    if layout is not None and layout["total"] is not None:
        meals_part = sheet_part(data, "Meals")
        mdoc = SheetDoc(parts[meals_part].decode("utf-8"))
        _apply_cells(mdoc, {ref: ("=0", 0) for ref in meals_formulas(layout, book)})
        replacements[meals_part] = mdoc.render().encode("utf-8")

    # Schema version marker (workbook defined name).
    workbook = parts["xl/workbook.xml"].decode("utf-8")
    marker = f'<definedName name="{SCHEMA_NAME}">{SCHEMA_VERSION}</definedName>'
    if SCHEMA_NAME in workbook:
        workbook = re.sub(rf'<definedName\b[^>]*\bname="{SCHEMA_NAME}"[^>]*>.*?</definedName>', marker, workbook, flags=re.S)
    elif re.search(r"<definedNames\s*/>", workbook):
        workbook = re.sub(r"<definedNames\s*/>", f"<definedNames>{marker}</definedNames>", workbook, count=1)
    elif "</definedNames>" in workbook:
        workbook = workbook.replace("</definedNames>", marker + "</definedNames>", 1)
    else:
        workbook = workbook.replace("</sheets>", "</sheets><definedNames>" + marker + "</definedNames>", 1)
    if "<calcPr" not in workbook:
        workbook = workbook.replace("</workbook>", '<calcPr fullCalcOnLoad="1" /></workbook>', 1)
    replacements["xl/workbook.xml"] = workbook.encode("utf-8")

    rebuilt = repack(data, replacements)
    rebuilt = refresh_caches(rebuilt)
    # Guard: only the intended parts differ.
    allowed = {daily_part, db_part, "xl/workbook.xml"} | ({sheet_part(data, "Meals")} if layout is not None else set())
    old, new = package_parts(data), package_parts(rebuilt)
    for name, payload in old.items():
        if name not in allowed and new.get(name) != payload:
            raise WorkbookWriteError("protected_sheet_changed", f"Migration changed unexpected part {name}")
    after = inspect_workbook(rebuilt)
    if after.sheets["Daily"].headers != base + [STATUS]:
        raise WorkbookWriteError("daily_headers", "Daily day_status header was not written")
    return rebuilt
