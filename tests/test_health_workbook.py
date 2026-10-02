"""Phase 1 read-only health workbook service. Fixture data only."""

import io
import json
import os
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request
import zipfile
from datetime import date
from pathlib import Path

from health_workbook.service import App, Config, build_logger, serve
from health_workbook.workbook import TABLE_HEADERS, load_workbook

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "phase1-test-token"
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def column_letter(index):
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def xml_text(value):
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def cell_xml(column, row, value):
    ref = f"{column_letter(column)}{row}"
    if isinstance(value, float):
        return f'<c r="{ref}"><v>{value}</v></c>'
    if isinstance(value, int) and not isinstance(value, bool):
        return f'<c r="{ref}"><v>{value}</v></c>'
    return f'<c r="{ref}" t="inlineStr"><is><t>{xml_text(value)}</t></is></c>'


def sheet_xml(rows):
    body = []
    for row_number, row in enumerate(rows, start=1):
        cells = "".join(
            cell_xml(column, row_number, value)
            for column, value in enumerate(row, start=1)
            if value is not None
        )
        if cells:
            body.append(f'<row r="{row_number}">{cells}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{NS}"><sheetData>{"".join(body)}</sheetData></worksheet>'
    )


def write_workbook(path, sheets):
    """sheets is an ordered list of (name, rows)."""
    overrides = []
    workbook_sheets = []
    rels = []
    parts = {}
    for index, (name, rows) in enumerate(sheets, start=1):
        part = f"xl/worksheets/sheet{index}.xml"
        parts[part] = sheet_xml(rows)
        overrides.append(
            f'<Override PartName="/{part}" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
        workbook_sheets.append(f'<sheet name="{xml_text(name)}" sheetId="{index}" r:id="rId{index}"/>')
        rels.append(
            f'<Relationship Id="rId{index}" '
            f'Type="{REL}/worksheet" Target="worksheets/sheet{index}.xml"/>'
        )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + "".join(overrides) + "</Types>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{NS}" xmlns:r="{REL}"><sheets>'
        + "".join(workbook_sheets) + "</sheets></workbook>"
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{PKG}">' + "".join(rels) + "</Relationships>"
    )
    package_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{PKG}">'
        f'<Relationship Id="rId1" Type="{REL}/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", package_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        for part, payload in parts.items():
            archive.writestr(part, payload)


def table(name, *records):
    header = list(TABLE_HEADERS[name])
    rows = [header]
    for record in records:
        rows.append([record.get(column) for column in header])
    return rows


def fixture_sheets(extra=()):
    sheets = [
        ("README", [
            ["Fixture README"],
            ["Deadline", "October 12, 2026"],
            ["Rule", "Do not invent numbers — fixture"],
        ]),
        ("Deficit Bank", [["Deficit Bank fixture"], ["Completed days only"]]),
        ("Weight Trend", [
            ["trend preamble"],
            list(TABLE_HEADERS["Weight Trend"]),
            ["2026-01-02", 100, 20, None, "first"],
            ["2026-01-03", 99, 20, -1, "second"],
            ["trend footer"],
        ]),
        ("Daily", table(
            "Daily",
            {"date": "2026-01-02", "steps": 10, "updated_by": "fixture"},
            {"date": "2026-01-03", "steps": 20, "updated_by": "fixture"},
            {"date": "2026-01-04", "steps": 30, "updated_by": "fixture"},
        )),
        ("Meals", table(
            "Meals",
            {"date": "2026-01-02", "time": "08:00", "food": "fixture-oats", "kcal": 100, "protein_g": 12.5, "updated_by": "fixture"},
            {"date": "2026-01-03", "time": "09:00", "food": "fixture-eggs", "kcal": 50, "updated_by": "fixture"},
        )),
        ("Training", table(
            "Training",
            {"date": "2026-01-02", "session_id": "S1", "source": "FITBIT", "type": "walk", "burn_role": "do_not_sum", "updated_by": "fixture"},
            {"date": "2026-01-02", "session_id": "S1", "source": "POLAR_H10", "type": "walk", "burn_role": "session_hr_burn", "updated_by": "fixture"},
            {"date": "2026-01-03", "session_id": "S2", "source": "TREADMILL", "type": "treadmill", "burn_role": "distance_climb", "updated_by": "fixture"},
        )),
        ("Lifts", table(
            "Lifts",
            {"date": "2026-01-02", "exercise": "fixture-squat", "reps_set1": 5, "weight_set1": 40, "updated_by": "fixture"},
        )),
        ("Notes", table(
            "Notes",
            {"datetime": "2026-01-02 08:00 AM", "category": "sleep", "severity": "mild", "updated_by": "fixture"},
            {"datetime": "2026-01-04 09:00 AM", "category": "meal", "severity": "moderate", "updated_by": "fixture"},
        )),
    ]
    return sheets + list(extra)


class HttpFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.path = Path(cls.tmp.name) / "staged.xlsx"
        write_workbook(cls.path, fixture_sheets())
        cls.before = cls.path.read_bytes()
        cls.logs = io.StringIO()
        cls.config = Config(cls.path, TOKEN, "127.0.0.1", 0)
        cls.server = serve(cls.config, build_logger(cls.logs))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def request(self, method, path, token=TOKEN, data=None, auth=True):
        headers = {}
        if auth:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                raw = response.read()
                return response.status, json.loads(raw.decode()) if raw else {}
        except urllib.error.HTTPError as error:
            try:
                raw = error.read()
                return error.code, json.loads(raw.decode()) if raw else {}
            finally:
                error.close()

    def test_status(self):
        status, body = self.request("GET", "/v1/status")
        self.assertEqual(status, 200)
        self.assertEqual(body["service"], "syzygy-health-workbook")
        self.assertEqual(body["phase"], 1)
        self.assertEqual(body["mode"], "read-only-staging")
        self.assertTrue(body["read_only"])
        self.assertFalse(body["writes_enabled"])
        self.assertFalse(body["public_route"])
        self.assertFalse(body["measurements_enabled"])
        self.assertTrue(body["valid"])
        self.assertEqual(body["host"], "127.0.0.1")
        self.assertEqual(body["unserved_sheets"], [])
        self.assertIn("Daily", body["sheets"])
        self.assertIn("README", body["sheets"])
        self.assertNotIn("Measurements", body["sheets"])

    def test_readme(self):
        status, body = self.request("GET", "/v1/readme")
        self.assertEqual(status, 200)
        self.assertEqual(body["sheet"], "README")
        self.assertTrue(body["read_only"])
        values = [value for row in body["rows"] for value in row["cells"].values()]
        self.assertIn("Deadline", values)
        self.assertIn("October 12, 2026", values)
        self.assertIn("Do not invent numbers — fixture", values)

    def test_daily_sheet(self):
        status, body = self.request("GET", "/v1/sheets/daily")
        self.assertEqual(status, 200)
        self.assertEqual(body["kind"], "table")
        self.assertEqual(body["headers"][0], "date")
        self.assertEqual(body["headers"][-1], "updated_by")
        self.assertEqual([row["date"] for row in body["rows"]], ["2026-01-02", "2026-01-03", "2026-01-04"])
        self.assertEqual(body["rows"][0]["steps"], 10)
        self.assertIsNone(body["rows"][0]["weight_lb"])
        self.assertTrue(body["read_only"])

    def test_meals_sheet(self):
        status, body = self.request("GET", "/v1/sheets/meals")
        self.assertEqual(status, 200)
        self.assertEqual(body["rows"][0]["food"], "fixture-oats")
        self.assertEqual(body["rows"][0]["kcal"], 100)
        self.assertEqual(body["rows"][0]["protein_g"], 12.5)

    def test_training_sheet(self):
        status, body = self.request("GET", "/v1/sheets/training")
        self.assertEqual(status, 200)
        self.assertEqual(
            [(row["source"], row["burn_role"]) for row in body["rows"]],
            [("FITBIT", "do_not_sum"), ("POLAR_H10", "session_hr_burn"), ("TREADMILL", "distance_climb")],
        )

    def test_lifts_sheet(self):
        status, body = self.request("GET", "/v1/sheets/lifts")
        self.assertEqual(status, 200)
        self.assertEqual(body["rows"][0]["exercise"], "fixture-squat")
        self.assertEqual(body["rows"][0]["reps_set1"], 5)
        self.assertEqual(body["rows"][0]["updated_by"], "fixture")

    def test_notes_sheet(self):
        status, body = self.request("GET", "/v1/sheets/notes")
        self.assertEqual(status, 200)
        self.assertEqual(body["rows"][0]["category"], "sleep")
        self.assertEqual(body["rows"][0]["severity"], "mild")
        self.assertEqual(body["headers"], list(TABLE_HEADERS["Notes"]))

    def test_weight_trend_sheet(self):
        status, body = self.request("GET", "/v1/sheets/weight-trend")
        self.assertEqual(status, 200)
        self.assertEqual(body["header_row"], 2)
        self.assertEqual(body["preamble"][0]["cells"]["A"], "trend preamble")
        self.assertEqual([row["date"] for row in body["rows"]], ["2026-01-02", "2026-01-03"])
        self.assertEqual(body["rows"][1]["delta_lb"], -1)
        self.assertEqual(body["footer"][0]["cells"]["A"], "trend footer")

    def test_deficit_bank_sheet(self):
        status, body = self.request("GET", "/v1/sheets/deficit-bank")
        self.assertEqual(status, 200)
        self.assertEqual(body["kind"], "prose")
        self.assertEqual(body["rows"][0]["cells"]["A"], "Deficit Bank fixture")

    def test_rows_by_date_range_session_and_limit(self):
        status, body = self.request("GET", "/v1/rows?sheet=daily&date=2026-01-03")
        self.assertEqual(status, 200)
        self.assertEqual([row["date"] for row in body["rows"]], ["2026-01-03"])
        status, body = self.request("GET", "/v1/rows?sheet=meals&from=2026-01-03&to=2026-01-04")
        self.assertEqual([row["food"] for row in body["rows"]], ["fixture-eggs"])
        status, body = self.request("GET", "/v1/rows?sheet=training&session_id=S1")
        self.assertEqual(status, 200)
        self.assertEqual(body["matched"], 2)
        self.assertEqual([row["source"] for row in body["rows"]], ["FITBIT", "POLAR_H10"])
        status, body = self.request("GET", "/v1/rows?sheet=notes&date=2026-01-02")
        self.assertEqual([row["category"] for row in body["rows"]], ["sleep"])
        status, body = self.request("GET", "/v1/rows?sheet=weight-trend&from=2026-01-03&to=2026-01-03")
        self.assertEqual([row["date"] for row in body["rows"]], ["2026-01-03"])
        status, body = self.request("GET", "/v1/rows?sheet=daily&limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(body["matched"], 3)
        self.assertTrue(body["limited"])
        self.assertEqual([row["date"] for row in body["rows"]], ["2026-01-03", "2026-01-04"])

    def test_unsupported_filters(self):
        status, body = self.request("GET", "/v1/rows?sheet=meals&session_id=S1")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "unsupported_filter")
        status, body = self.request("GET", "/v1/rows?sheet=deficit-bank&date=2026-01-02")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "unsupported_filter")
        status, body = self.request("GET", "/v1/rows?sheet=daily&date=2026-13-40")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_date")
        status, body = self.request("GET", "/v1/rows?sheet=daily&limit=0")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_limit")

    def test_auth_and_read_only_mode(self):
        status, body = self.request("GET", "/v1/status", auth=False)
        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")
        status, body = self.request("GET", "/v1/sheets/daily", token="wrong")
        self.assertEqual(status, 401)
        status, body = self.request("GET", "/v1/status?token=" + TOKEN)
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "token_in_url")
        for method, path in (
            ("POST", "/v1/measurements"),
            ("PUT", "/v1/daily/2026-01-02"),
            ("PATCH", "/v1/lifts/lifts:2"),
            ("PUT", "/v1/master/workbook"),
            ("DELETE", "/v1/sheets/notes"),
        ):
            status, body = self.request(method, path, data=b"blocked")
            self.assertEqual(status, 405, path)
            self.assertEqual(body["error"], "read_only")
            self.assertTrue(body["read_only"])
            self.assertFalse(body["writes_enabled"])
        status, body = self.request("GET", "/v1/master/workbook")
        self.assertEqual(status, 404)
        status, body = self.request("GET", "/v1/sheets/measurements")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "measurements_unavailable")
        status, body = self.request("GET", "/v1/rows?sheet=measurements")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "measurements_unavailable")

    def test_logs_omit_token_and_cell_values(self):
        self.request("GET", "/v1/sheets/meals")
        text = self.logs.getvalue()
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("fixture-oats", text)
        self.assertIn('"path": "/v1/sheets/meals"', text)
        self.assertIn('"status": 200', text)

    def test_reads_do_not_change_the_staged_file(self):
        self.assertEqual(self.path.read_bytes(), self.before)
        names = {path.name for path in Path(self.tmp.name).iterdir()}
        self.assertEqual(names, {"staged.xlsx"})


class WorkbookValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def app(self, path):
        return App(Config(path, TOKEN), build_logger(io.StringIO()))

    def test_missing_sheet_is_invalid(self):
        path = Path(self.tmp.name) / "missing.xlsx"
        sheets = [item for item in fixture_sheets() if item[0] != "Notes"]
        write_workbook(path, sheets)
        result = load_workbook(path)
        self.assertFalse(result.valid)
        self.assertIn("Notes", result.detail)
        status, body = self.app(path).dispatch("GET", "/v1/sheets/daily", {"Authorization": "Bearer " + TOKEN})
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "workbook_invalid")
        status, body = self.app(path).dispatch("GET", "/v1/status", {"Authorization": "Bearer " + TOKEN})
        self.assertEqual(status, 200)
        self.assertFalse(body["valid"])

    def test_header_mismatch_and_corrupt_file(self):
        path = Path(self.tmp.name) / "bad-header.xlsx"
        sheets = fixture_sheets()
        daily = next(rows for name, rows in sheets if name == "Daily")
        daily[0][0] = "day"
        write_workbook(path, sheets)
        result = load_workbook(path)
        self.assertFalse(result.valid)
        self.assertIn("Daily header mismatch", result.detail)
        corrupt = Path(self.tmp.name) / "corrupt.xlsx"
        corrupt.write_bytes(b"not a workbook")
        result = load_workbook(corrupt)
        self.assertEqual(result.error, "workbook_invalid")
        status, _body = self.app(corrupt).dispatch("GET", "/v1/readme", {"Authorization": "Bearer " + TOKEN})
        self.assertEqual(status, 503)

    def test_missing_file_and_directory_are_rejected(self):
        missing = Path(self.tmp.name) / "absent.xlsx"
        result = load_workbook(missing)
        self.assertFalse(result.present)
        self.assertEqual(result.error, "workbook_missing")
        result = load_workbook(Path(self.tmp.name))
        self.assertEqual(result.error, "workbook_not_regular_file")

    def test_extra_measurements_sheet_stays_unserved(self):
        path = Path(self.tmp.name) / "extra.xlsx"
        write_workbook(path, fixture_sheets(extra=[("Measurements", [["metric", "value"], ["blood_glucose", 1]])]))
        result = load_workbook(path)
        self.assertTrue(result.valid)
        status, body = self.app(path).dispatch("GET", "/v1/status", {"Authorization": "Bearer " + TOKEN})
        self.assertEqual(body["unserved_sheets"], ["Measurements"])
        self.assertFalse(body["measurements_enabled"])
        status, body = self.app(path).dispatch("GET", "/v1/sheets/measurements", {"Authorization": "Bearer " + TOKEN})
        self.assertEqual(status, 404)
        status, body = self.app(path).dispatch("GET", "/v1/sheets/daily", {"Authorization": "Bearer " + TOKEN})
        self.assertEqual(status, 200)

    def test_shared_strings_and_excel_dates(self):
        serial = (date(2026, 1, 2) - date(1899, 12, 30)).days
        path = Path(self.tmp.name) / "shared.xlsx"
        sheets = fixture_sheets()
        # Replace the meals food string with a shared-string index after the zip is built.
        write_workbook(path, sheets)
        with zipfile.ZipFile(path, "r") as archive:
            payload = {name: archive.read(name) for name in archive.namelist()}
        meals = payload["xl/worksheets/sheet5.xml"].decode().replace(
            '<c r="C2" t="inlineStr"><is><t>fixture-oats</t></is></c>',
            '<c r="C2" t="s"><v>0</v></c>',
        )
        daily = payload["xl/worksheets/sheet4.xml"].decode().replace(
            '<c r="A2" t="inlineStr"><is><t>2026-01-02</t></is></c>',
            f'<c r="A2"><v>{serial}</v></c>',
        )
        self.assertIn('t="s"', meals)
        self.assertIn(f"<v>{serial}</v>", daily)
        payload["xl/worksheets/sheet5.xml"] = meals.encode()
        payload["xl/worksheets/sheet4.xml"] = daily.encode()
        payload["xl/sharedStrings.xml"] = (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<sst xmlns="{NS}"><si><t>fixture-oats</t></si></sst>'
        ).encode()
        with zipfile.ZipFile(path, "w") as archive:
            for name, blob in payload.items():
                archive.writestr(name, blob)
        result = load_workbook(path)
        self.assertTrue(result.valid, result.detail)
        meals_sheet = result.workbook.sheets["Meals"]
        daily_sheet = result.workbook.sheets["Daily"]
        self.assertEqual(meals_sheet.rows[0]["food"], "fixture-oats")
        self.assertEqual(daily_sheet.rows[0]["date"], "2026-01-02")

    def test_loopback_is_required(self):
        with mock.patch.dict(
            os.environ,
            {
                "HEALTH_WORKBOOK_HOST": "0.0.0.0",
                "HEALTH_WORKBOOK_READ_TOKEN": TOKEN,
                "HEALTH_WORKBOOK_PATH": str(Path(self.tmp.name) / "absent.xlsx"),
            },
            clear=False,
        ):
            with self.assertRaises(SystemExit):
                Config.from_env()

    def test_phase1_source_has_no_write_path(self):
        text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "health_workbook").glob("*.py")
        )
        for banned in ("os.replace", "write_bytes", "NamedTemporaryFile", "wb'"):
            self.assertNotIn(banned, text)

