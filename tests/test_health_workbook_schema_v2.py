"""Schema v2: Meals footer-safe insertion, Daily.day_status migration, formula-driven deficit logic."""

import tempfile
import unittest
from pathlib import Path

from health_workbook import formulas
from health_workbook.mutate import append_row, package_parts, patch_row
from health_workbook.store import WriteStore
from health_workbook.workbook import TABLE_HEADERS, inspect_workbook
from tests.test_health_workbook import fixture_sheets, table, write_workbook

ATTR = {"source": "MANUAL", "updated_by": "tester", "recorded_at": "2026-10-04T00:00:00Z"}


def meal(day, time, food, kcal):
    return {"date": day, "time": time, "food": food, "kcal": kcal, "protein_g": 1, "carbs_g": 2, "fat_g": 3, **ATTR}


def daily(day, kcal=None, burn=None, **extra):
    return {"date": day, "kcal_in": kcal, "fitbit_total_burn": burn, **extra, **ATTR}


def meals_sheet(footer=True):
    rows = table("Meals",
                 {"date": "2026-09-17", "time": "11:00:00 AM", "food": "a", "kcal": 100, "protein_g": 1, "carbs_g": 2, "fat_g": 3},
                 {"date": "2026-09-18", "time": "11:00:00 AM", "food": "b", "kcal": 200, "protein_g": 1, "carbs_g": 2, "fat_g": 3})
    if footer:
        rows += [[None], ["TOTAL", None, "(all meal rows above)", 300, 2, 4, 6], [None], ["DAILY TOTALS"],
                 ["date", "kcal", "protein_g", "carbs_g", "fat_g"], ["2026-09-17", 100, 1, 2, 3], ["2026-09-18", 200, 1, 2, 3]]
    return rows


def build(tmp, footer=True):
    sheets = [s for s in fixture_sheets() if s[0] != "Meals"]
    sheets.append(("Meals", meals_sheet(footer)))
    path = Path(tmp) / "wb.xlsx"
    write_workbook(path, sheets)
    return path


class MealsInsertTests(unittest.TestCase):
    def test_new_meal_lands_above_unmigrated_footer(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build(tmp)
            data = path.read_bytes()
            before = inspect_workbook(data).sheets["Meals"]
            new, row_id = append_row(data, "Meals", meal("2026-09-19", "01:00:00 PM", "c", 50))
            sheet = inspect_workbook(new).sheets["Meals"]
            self.assertEqual(row_id, "meals:4")
            self.assertEqual([r["row"] for r in sheet.rows], [2, 3, 4])
            self.assertEqual([f["row"] for f in sheet.footer], [f["row"] + 1 for f in before.footer])
            self.assertEqual(sheet.footer[0]["cells"]["A"], "TOTAL")  # static footer stays static, just shifted
            self.assertEqual(sheet.footer[0]["cells"]["D"], 300)

    def test_many_inserts_keep_order_and_footer(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = build(tmp).read_bytes()
            for i in range(5):
                data, _ = append_row(data, "Meals", meal("2026-09-19", f"0{i}:00:00 PM", f"m{i}", 10))
            sheet = inspect_workbook(data).sheets["Meals"]
            self.assertEqual([r["row"] for r in sheet.rows], list(range(2, 9)))
            self.assertEqual(sheet.footer[0]["cells"]["A"], "TOTAL")
            self.assertEqual(sheet.footer[-1]["cells"]["A"], "2026-09-18")

    def test_no_footer_behaves_like_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = build(tmp, footer=False).read_bytes()
            new, row_id = append_row(data, "Meals", meal("2026-09-19", "01:00:00 PM", "c", 50))
            self.assertEqual(row_id, "meals:4")

    def test_refuses_to_shift_unmanaged_formulas(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build(tmp)
            parts = package_parts(path.read_bytes())
            from health_workbook.pkg import sheet_part
            part = sheet_part(path.read_bytes(), "Meals")
            xml = parts[part].decode()
            xml = xml.replace("</sheetData>", '<row r="20"><c r="A20"><f>1+1</f><v>2</v></c></row></sheetData>')
            from health_workbook.mutate import _repack
            data = _repack(path.read_bytes(), {part: xml.encode()})
            from health_workbook.mutate import WorkbookWriteError
            with self.assertRaises(WorkbookWriteError) as ctx:
                append_row(data, "Meals", meal("2026-09-19", "01:00:00 PM", "c", 50))
            self.assertEqual(ctx.exception.code, "meals_formulas_unsupported")


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = build(self.tmp.name)
        self.store = WriteStore(self.path)

    def test_migration_is_idempotent_and_backed_up(self):
        first = self.store.migrate_schema_v2()
        self.assertEqual(first["result"], "migrated")
        self.assertTrue((self.path.parent / "backups" / first["backup"]).exists())
        digest = self.path.read_bytes()
        second = self.store.migrate_schema_v2()
        self.assertEqual(second["result"], "present")
        self.assertEqual(self.path.read_bytes(), digest)
        self.assertEqual(formulas.schema_version(digest), 2)
        self.assertEqual(inspect_workbook(digest).sheets["Daily"].headers, list(TABLE_HEADERS["Daily"]) + ["day_status"])

    def test_day_status_needs_migration_then_validates(self):
        from health_workbook.mutate import WorkbookWriteError
        with self.assertRaises(WorkbookWriteError) as ctx:
            self.store.append_sheet("Daily", daily("2026-09-20", 1000, 3000, day_status="complete"))
        self.assertEqual(ctx.exception.code, "schema_outdated")
        self.store.migrate_schema_v2()
        with self.assertRaises(WorkbookWriteError) as ctx:
            self.store.append_sheet("Daily", daily("2026-09-20", 1000, 3000, day_status="closed"))
        self.assertEqual(ctx.exception.code, "invalid_day_status")
        with self.assertRaises(WorkbookWriteError) as ctx:
            self.store.append_sheet("Daily", daily("2026-09-20", 1000, 3000, fitbit_deficit=5))
        self.assertEqual(ctx.exception.code, "formula_field")

    def test_only_complete_days_contribute_and_formulas_survive_writes(self):
        self.store.migrate_schema_v2()
        self.store.append_sheet("Daily", daily("2026-09-20", 1500, 3000, day_status="complete"))
        self.store.append_sheet("Daily", daily("2026-09-21", 2000, 3500, day_status="open"))
        self.store.append_sheet("Daily", daily("2026-09-22", 1900, 3300))
        self.store.append_sheet("Daily", daily("2026-09-19", 1000, 2800, day_status="complete"))  # out of order
        rows = {r["date"]: r for r in inspect_workbook(self.path.read_bytes()).sheets["Daily"].rows}
        self.assertEqual(rows["2026-09-20"]["fitbit_deficit"], 1500)
        self.assertEqual(rows["2026-09-20"]["under_1800"], 300)
        self.assertEqual(rows["2026-09-20"]["bank_under_1800_cum"], 300 + 800)  # includes earlier-dated complete row
        self.assertEqual(rows["2026-09-19"]["bank_under_1800_cum"], 800)
        self.assertIn(rows["2026-09-21"]["under_1800"], (None, ""))
        self.assertIn(rows["2026-09-22"]["bank_deficit_cum"], (None, ""))
        # a normal PATCH (feeder style) must keep formulas and statuses; flipping status re-gates
        before = package_parts(self.path.read_bytes())
        self.store.correct_sheet("Daily", rows["2026-09-21"]["row_id"], {"fields": {"steps": 5000}, "reason": "r", **ATTR})
        after = package_parts(self.path.read_bytes())
        from health_workbook.pkg import sheet_part
        xml = after[sheet_part(self.path.read_bytes(), "Daily")].decode()
        self.assertEqual(xml.count("<f>"), 6 * len(rows))
        self.store.correct_sheet("Daily", rows["2026-09-21"]["row_id"], {"fields": {"day_status": "complete"}, "reason": "r", **ATTR})
        rows = {r["date"]: r for r in inspect_workbook(self.path.read_bytes()).sheets["Daily"].rows}
        self.assertEqual(rows["2026-09-21"]["under_1800"], -200)
        self.assertEqual(rows["2026-09-21"]["bank_under_1800_cum"], 800 + 300 - 200)
        self.assertEqual(rows["2026-09-21"]["steps"], 5000)
        db = inspect_workbook(self.path.read_bytes()).sheets["Deficit Bank"]
        cells = {r["row"]: r["cells"] for r in db.rows}
        self.assertEqual(cells[8]["B"], 3)
        self.assertEqual(cells[12]["B"], 900)

    def test_other_sheet_writes_never_touch_daily_or_meals(self):
        from health_workbook.pkg import sheet_part
        self.store.migrate_schema_v2()
        self.store.append_sheet("Daily", daily("2026-09-20", 1500, 3000, day_status="complete"))
        data = self.path.read_bytes()
        part = sheet_part(data, "Daily")
        stale = package_parts(data)[part].decode().replace("<v>300</v>", "<v>999</v>")
        self.assertNotEqual(stale, package_parts(data)[part].decode())
        from health_workbook.mutate import _repack
        self.path.write_bytes(_repack(data, {part: stale.encode()}))
        self.store.append_sheet("Notes", {"datetime": "2026-09-20 10:00", "category": "c", "note_paraphrased": "n", **ATTR})
        self.assertEqual(package_parts(self.path.read_bytes())[part].decode(), stale)

    def test_meals_footer_becomes_formulas_and_tracks_inserts(self):
        self.store.migrate_schema_v2()
        self.store.append_sheet("Meals", meal("2026-09-18", "02:00:00 PM", "extra", 50))
        self.store.append_sheet("Meals", meal("2026-09-19", "02:00:00 PM", "newday", 70))
        sheet = inspect_workbook(self.path.read_bytes()).sheets["Meals"]
        self.assertEqual([r["row"] for r in sheet.rows], [2, 3, 4, 5])
        footer = {f["cells"]["A"]: f["cells"] for f in sheet.footer if "A" in f["cells"]}
        self.assertEqual(footer["TOTAL"]["D"], 420)
        self.assertEqual(footer["2026-09-18"]["B"], 250)
        self.assertEqual(footer["2026-09-19"]["B"], 70)  # auto-added DAILY TOTALS line for a new date
        self.assertEqual(sheet.footer[0]["cells"]["A"], "TOTAL")


if __name__ == "__main__":
    unittest.main()
