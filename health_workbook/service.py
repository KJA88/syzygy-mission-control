"""Loopback health workbook service. Phase 2A writes Measurements only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import logging
import os
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

from health_workbook.mutate import WorkbookWriteError
from health_workbook.store import WriteFault, WriteStore
from health_workbook.workbook import (
    DATE_FIELDS,
    REQUIRED_SHEETS,
    LoadResult,
    load_workbook,
    resolve_sheet_name,
)

SERVICE = "syzygy-health-workbook"
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
ROW_FILTERS = {"sheet", "date", "from", "to", "session_id", "limit"}
TOKEN_QUERY_KEYS = {"token", "access_token", "authorization"}


class ApiError(Exception):
    def __init__(self, status: int, error: str, detail: str | None = None):
        super().__init__(detail or error)
        self.status = status
        self.error = error
        self.detail = detail


@dataclass(frozen=True)
class Config:
    workbook_path: Path
    read_token: str
    host: str = "127.0.0.1"
    port: int = 5052
    maintain_token: str = ""
    fault: str | None = None

    def __post_init__(self) -> None:
        if self.maintain_token and self.maintain_token == self.read_token:
            raise ValueError("Read and maintain tokens must differ")

    @classmethod
    def from_env(cls) -> Config:
        token = os.environ.get("HEALTH_WORKBOOK_READ_TOKEN", "")
        maintain = os.environ.get("HEALTH_WORKBOOK_MAINTAIN_TOKEN", "")
        path = os.environ.get("HEALTH_WORKBOOK_PATH", "")
        host = os.environ.get("HEALTH_WORKBOOK_HOST", "127.0.0.1")
        port_text = os.environ.get("HEALTH_WORKBOOK_PORT", "5052")
        if not token:
            raise SystemExit("HEALTH_WORKBOOK_READ_TOKEN is required")
        if not maintain:
            raise SystemExit("HEALTH_WORKBOOK_MAINTAIN_TOKEN is required")
        if token == maintain:
            raise SystemExit("Read and maintain tokens must differ")
        if not path:
            raise SystemExit("HEALTH_WORKBOOK_PATH is required")
        if host != "127.0.0.1":
            raise SystemExit("HEALTH_WORKBOOK_HOST must be 127.0.0.1")
        try:
            port = int(port_text)
        except ValueError as error:
            raise SystemExit("HEALTH_WORKBOOK_PORT must be an integer") from error
        if port < 1 or port > 65535:
            raise SystemExit("HEALTH_WORKBOOK_PORT must be an integer")
        return cls(Path(path), token, host, port, maintain)


class JsonFormatter(logging.Formatter):
    _FIELDS = ("method", "path", "status", "sheet", "rows")

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        for key in self._FIELDS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, sort_keys=True)


def build_logger(stream=None) -> logging.Logger:
    logger = logging.getLogger("syzygy.health_workbook")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger


class App:
    def __init__(self, config: Config, logger: logging.Logger):
        self.config = config
        self.logger = logger

    def dispatch(self, method: str, target: str, headers, body: bytes = b"") -> tuple[int, dict]:
        parts = urlsplit(target)
        path = parts.path[:-1] if parts.path.endswith("/") and parts.path != "/" else parts.path
        sheet_name = None
        row_count = None
        try:
            role = self._authorize(headers)
            self._reject_token_query(parts.query)
            if method in {"GET", "HEAD"}:
                status, payload = self._get(path, parts.query)
            else:
                status, payload = self._write(method, path, role, body)
            sheet_name = payload.get("sheet")
            rows = payload.get("rows")
            row_count = len(rows) if isinstance(rows, list) else None
            response = payload
        except ApiError as error:
            status = error.status
            response = {"error": error.error}
            if error.detail:
                response["detail"] = error.detail
            if error.error == "writes_disabled":
                response["writes_enabled"] = ["measurements"]
        except WorkbookWriteError as error:
            status = 409 if error.code in {"measurements_missing", "conflict"} else 400
            response = {"error": error.code, "detail": error.detail}
        except WriteFault:
            status = 500
            response = {"error": "write_failed"}
        except Exception as error:
            status = 500
            response = {"error": "internal_error"}
            self.logger.error("internal_error %s", type(error).__name__)
        self.logger.info(
            "request",
            extra={
                "method": method,
                "path": path,
                "status": status,
                "sheet": sheet_name,
                "rows": row_count,
            },
        )
        return status, response

    def _authorize(self, headers) -> str:
        supplied = headers.get("Authorization", "")
        if not isinstance(supplied, str) or len(supplied) > 500:
            raise ApiError(401, "unauthorized")
        maintain = "Bearer " + self.config.maintain_token if self.config.maintain_token else ""
        reader = "Bearer " + self.config.read_token
        if maintain and hmac.compare_digest(supplied, maintain):
            return "maintain"
        if hmac.compare_digest(supplied, reader):
            return "read"
        raise ApiError(401, "unauthorized")

    def _reject_token_query(self, query: str) -> None:
        parsed = parse_qs(query, keep_blank_values=True)
        if TOKEN_QUERY_KEYS.intersection({key.casefold() for key in parsed}):
            raise ApiError(400, "token_in_url", "Send the bearer token in the Authorization header")

    def _get(self, path: str, query: str) -> tuple[int, dict]:
        if path == "/v1/status":
            self._reject_unexpected(query, set())
            return 200, self._status()
        loaded = self._loaded()
        if path == "/v1/readme":
            self._reject_unexpected(query, set())
            sheet = loaded.workbook.sheets["README"]
            return 200, self._envelope("README", "prose", rows=sheet.rows)
        if path == "/v1/rows":
            return 200, self._rows(loaded, query)
        prefix = "/v1/sheets/"
        if path.startswith(prefix):
            self._reject_unexpected(query, set())
            return 200, self._sheet(loaded, path[len(prefix):])
        raise ApiError(404, "not_found")

    def _status(self) -> dict:
        loaded = load_workbook(self.config.workbook_path)
        workbook = loaded.workbook
        order = workbook.sheet_order if workbook else []
        return {
            "service": SERVICE,
            "phase": "2a",
            "mode": "measurements-write-staging",
            "read_only": False,
            "writes_enabled": ["measurements"],
            "host": self.config.host,
            "public_route": False,
            "measurements_enabled": "Measurements" in order,
            "workbook_present": loaded.present,
            "workbook_name": self.config.workbook_path.name if loaded.present else None,
            "sha256": workbook.sha256 if workbook else None,
            "valid": loaded.valid,
            "error": loaded.error,
            "detail": loaded.detail,
            "sheets": [name for name in order if name in REQUIRED_SHEETS or name == "Measurements"],
            "unserved_sheets": [name for name in order if name not in REQUIRED_SHEETS and name != "Measurements"],
        }

    def _loaded(self) -> LoadResult:
        loaded = load_workbook(self.config.workbook_path)
        if not loaded.valid or loaded.workbook is None:
            raise ApiError(503, loaded.error or "workbook_invalid", loaded.detail)
        return loaded

    def _write(self, method: str, path: str, role: str, raw: bytes) -> tuple[int, dict]:
        if role != "maintain":
            raise ApiError(403, "forbidden", "Maintain permission is required")
        if len(raw) > 1024 * 1024:
            raise ApiError(413, "body_too_large")
        if method == "POST" and path == "/v1/measurements":
            return self._measurement_result(self._store().append(self._json_object(raw)), 201)
        prefix = "/v1/measurements/"
        if method == "PATCH" and path.startswith(prefix):
            result = self._store().correct(path[len(prefix):], self._json_object(raw))
            return self._measurement_result(result, 200)
        raise ApiError(405, "writes_disabled", "Only Measurements writes are enabled")

    def _json_object(self, raw: bytes) -> dict:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ApiError(400, "malformed", "Request body must be JSON") from error
        if not isinstance(payload, dict):
            raise ApiError(400, "malformed", "Request body must be an object")
        return payload

    def _store(self) -> WriteStore:
        return WriteStore(self.config.workbook_path, self.config.fault)

    def _measurement_result(self, result: dict, created: int) -> tuple[int, dict]:
        status = 200 if result["result"] == "exists" else created
        result["read_only"] = False
        return status, result

    def _sheet(self, loaded: LoadResult, key: str) -> dict:
        normalized = key.casefold().replace("_", "-")
        if normalized in {"measurements", "measurement"} and "Measurements" not in loaded.workbook.sheets:
            raise ApiError(404, "measurements_unavailable", "Measurements sheet is not in this workbook")
        name = resolve_sheet_name(key)
        if name is None or name == "README" or (name not in REQUIRED_SHEETS and name != "Measurements"):
            raise ApiError(404, "unknown_sheet")
        sheet = loaded.workbook.sheets[name]
        if sheet.kind == "prose":
            return self._envelope(name, "prose", rows=sheet.rows)
        return self._envelope(
            name,
            "table",
            headers=sheet.headers,
            header_row=sheet.header_row,
            preamble=sheet.preamble,
            rows=sheet.rows,
            footer=sheet.footer,
        )

    def _rows(self, loaded: LoadResult, query: str) -> dict:
        params = self._params(query, ROW_FILTERS)
        raw_sheet = params.get("sheet", "")
        if not raw_sheet:
            raise ApiError(400, "sheet_required")
        normalized = raw_sheet.casefold().replace("_", "-").replace(" ", "-")
        if normalized in {"measurements", "measurement"} and "Measurements" not in loaded.workbook.sheets:
            raise ApiError(404, "measurements_unavailable", "Measurements sheet is not in this workbook")
        name = resolve_sheet_name(raw_sheet)
        if name is None or (name not in REQUIRED_SHEETS and name != "Measurements"):
            raise ApiError(404, "unknown_sheet")
        date = _optional_day(params.get("date"), "date")
        start = _optional_day(params.get("from"), "from")
        end = _optional_day(params.get("to"), "to")
        session_id = params.get("session_id")
        limit = _limit(params.get("limit"))
        if name not in DATE_FIELDS and any(value is not None for value in (date, start, end)):
            raise ApiError(400, "unsupported_filter", f"{name} has no date column")
        if session_id is not None and name != "Training":
            raise ApiError(400, "unsupported_filter", "session_id applies to Training only")
        sheet = loaded.workbook.sheets[name]
        matched = [
            row for row in sheet.rows
            if _matches(row, DATE_FIELDS.get(name), date, start, end, session_id)
        ]
        window = matched[-limit:]
        body = self._envelope(name, sheet.kind, rows=window)
        body.update({
            "matched": len(matched),
            "limit": limit,
            "limited": len(matched) > len(window),
            "filters": {
                "date": date,
                "from": start,
                "to": end,
                "session_id": session_id,
            },
        })
        return body

    def _envelope(self, name: str, kind: str, **payload) -> dict:
        body = {
            "service": SERVICE,
            "read_only": name != "Measurements",
            "sheet": name,
            "kind": kind,
        }
        body.update(payload)
        return body

    def _params(self, query: str, allowed: set[str]) -> dict[str, str]:
        self._reject_unexpected(query, allowed)
        parsed = parse_qs(query, keep_blank_values=True)
        if any(len(values) != 1 for values in parsed.values()):
            raise ApiError(400, "repeated_parameter")
        return {key: values[0] for key, values in parsed.items()}

    def _reject_unexpected(self, query: str, allowed: set[str]) -> None:
        parsed = parse_qs(query, keep_blank_values=True)
        unknown = set(parsed).difference(allowed)
        if unknown:
            raise ApiError(400, "unexpected_parameter", "Unexpected parameter: " + ", ".join(sorted(unknown)))
        if any(len(values) != 1 for values in parsed.values()):
            raise ApiError(400, "repeated_parameter")


def _optional_day(value: str | None, label: str) -> str | None:
    if value is None or value == "":
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as error:
        raise ApiError(400, "invalid_date", f"{label} must be YYYY-MM-DD") from error
    return value


def _limit(value: str | None) -> int:
    if value is None or value == "":
        return DEFAULT_LIMIT
    if not value.isdigit():
        raise ApiError(400, "invalid_limit", f"limit must be 1..{MAX_LIMIT}")
    number = int(value)
    if number < 1 or number > MAX_LIMIT:
        raise ApiError(400, "invalid_limit", f"limit must be 1..{MAX_LIMIT}")
    return number


def _matches(row: dict, field: str | None, date: str | None, start: str | None, end: str | None, session_id: str | None) -> bool:
    if session_id is not None and str(row.get("session_id") or "") != session_id:
        return False
    if date is None and start is None and end is None:
        return True
    day = _day_of(row.get(field) if field else None)
    if day is None:
        return False
    if date is not None and day != date:
        return False
    if start is not None and day < start:
        return False
    if end is not None and day > end:
        return False
    return True


def _day_of(value: object) -> str | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    head = value[:10]
    try:
        datetime.strptime(head, "%Y-%m-%d")
    except ValueError:
        return None
    return head


def build_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self._respond()

        def do_HEAD(self):
            self._respond()

        def do_POST(self):
            self._respond()

        def do_PUT(self):
            self._respond()

        def do_PATCH(self):
            self._respond()

        def do_DELETE(self):
            self._respond()

        def _respond(self):
            raw = self._read_body() if self.command in {"POST", "PUT", "PATCH", "DELETE"} else b""
            status, body = app.dispatch(self.command, self.path, self.headers, raw)
            encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(encoded)

        def _read_body(self) -> bytes:
            raw_length = self.headers.get("Content-Length", "0")
            try:
                length = int(raw_length)
            except (TypeError, ValueError):
                length = 0
            if length < 0:
                return b""
            remaining = min(length, 1024 * 1024 + 1)
            chunks = []
            while remaining:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)

        def log_message(self, fmt, *args):
            return

    return Handler


def serve(config: Config, logger: logging.Logger | None = None) -> ThreadingHTTPServer:
    if config.host != "127.0.0.1":
        raise SystemExit("HEALTH_WORKBOOK_HOST must be 127.0.0.1")
    app = App(config, logger or build_logger())
    server = ThreadingHTTPServer((config.host, config.port), build_handler(app))
    server.app = app
    return server


def main() -> None:
    config = Config.from_env()
    logger = build_logger()
    server = serve(config, logger)
    logger.info(
        "listening",
        extra={"method": "START", "path": f"{config.host}:{config.port}", "status": 0},
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
