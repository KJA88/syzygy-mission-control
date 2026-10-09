"""Health capabilities. Every call goes to the Health Workbook Service."""
from __future__ import annotations

import json
import re
from urllib import error, parse, request

PROTECTED = {"readme", "weight trend", "deficit bank"}
READ_SLUG = {
    "readme": "readme",
    "daily": "daily",
    "meals": "meals",
    "training": "training",
    "lifts": "lifts",
    "notes": "notes",
    "weight trend": "weight-trend",
    "deficit bank": "deficit-bank",
    "measurements": "measurements",
}
WRITE_SLUG = {
    "measurements": "measurements",
    "daily": "daily",
    "meals": "meals",
    "training": "training",
    "lifts": "lifts",
    "notes": "notes",
}
PROVENANCE = ("source", "updated_by", "recorded_at")
BLOCKED_FIELDS = {"delete", "replace", "workbook", "path", "file", "xlsx"}
ROW_FILTERS = ("date", "from", "to", "session_id", "limit")


class HealthError(Exception):
    def __init__(self, code: str, detail: str | None = None, row_id: str | None = None):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail
        self.row_id = row_id


def sheet_key(value: object) -> str:
    return re.sub(r"[\s_-]+", " ", str(value or "")).strip().casefold()


class HealthBridge:
    def __init__(self, base: str, read_token: str, maintain_token: str, transport=None):
        self.base = base.rstrip("/")
        self.read_token = read_token
        self.maintain_token = maintain_token
        self.transport = transport or _urllib_transport

    def read(self, arguments: dict) -> dict:
        sheet = arguments.get("sheet")
        key = sheet_key(sheet)
        slug = READ_SLUG.get(key)
        if slug is None:
            raise HealthError("unknown_sheet", "That sheet is not served")
        filters = {name: arguments[name] for name in ROW_FILTERS if arguments.get(name) not in (None, "")}
        if slug == "readme":
            if filters:
                raise HealthError("unsupported_filter", "README does not accept row filters")
            return self._call("GET", "/v1/readme", "read", None, None)
        if filters:
            query = {"sheet": slug, **{key: str(value) for key, value in filters.items()}}
            return self._call("GET", "/v1/rows", "read", query, None)
        return self._call("GET", "/v1/sheets/" + slug, "read", None, None)

    def write(self, arguments: dict) -> dict:
        if BLOCKED_FIELDS.intersection(arguments):
            raise HealthError("writes_disabled", "Delete and replace are not capabilities")
        values = arguments.get("values")
        if not isinstance(values, dict) or not values:
            raise HealthError("malformed", "values must be an object")
        if BLOCKED_FIELDS.intersection(values):
            raise HealthError("writes_disabled", "Delete and replace are not capabilities")
        key = sheet_key(arguments.get("sheet"))
        if key in PROTECTED:
            raise HealthError("protected_sheet", "README, Weight Trend, and Deficit Bank are read-only")
        slug = WRITE_SLUG.get(key)
        if slug is None:
            raise HealthError("unknown_sheet", "That sheet does not accept writes")
        body = dict(values)
        for field in PROVENANCE:
            text = arguments.get(field)
            if not isinstance(text, str) or not text.strip():
                raise HealthError("missing_provenance", field + " is required")
            body[field] = text.strip()
        row_id = arguments.get("row_id")
        if row_id in (None, ""):
            return self._call("POST", "/v1/" + slug, "maintain", None, body)
        if not isinstance(row_id, str) or "/" in row_id:
            raise HealthError("malformed", "row_id is invalid")
        reason = arguments.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise HealthError("missing_provenance", "reason is required to correct a row")
        body["reason"] = reason.strip()
        body["fields"] = values
        return self._call("PATCH", "/v1/" + slug + "/" + parse.quote(row_id, safe=":"), "maintain", None, body)

    def audit(self, arguments: dict) -> dict:
        query = {}
        if arguments.get("limit") not in (None, ""):
            query["limit"] = str(arguments["limit"])
        if arguments.get("sheet"):
            query["sheet"] = str(arguments["sheet"])
        return self._call("GET", "/v1/audit", "read", query or None, None)

    def _call(self, method: str, path: str, role: str, query, body) -> dict:
        token = self.maintain_token if role == "maintain" else self.read_token
        if not token:
            raise HealthError("config", "missing " + role + " token")
        url = self.base + path
        if query:
            url += "?" + parse.urlencode(query)
        status, payload = self.transport(method, url, token, body)
        if status >= 400:
            code = payload.get("error") if isinstance(payload, dict) else "service_error"
            detail = payload.get("detail") if isinstance(payload, dict) else None
            row_id = payload.get("row_id") if isinstance(payload, dict) else None
            raise HealthError(
                str(code or "service_error"),
                _scrub(detail, self.read_token, self.maintain_token),
                row_id if isinstance(row_id, str) else None,
            )
        return payload


def _urllib_transport(method: str, url: str, token: str, body):
    data = None
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=15) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {"error": "service_error"}
        return exc.code, payload
    except Exception as exc:
        return 503, {"error": "unreachable", "detail": type(exc).__name__}


def _scrub(value, read_token: str, maintain_token: str):
    if not isinstance(value, str):
        return value
    for token in (read_token, maintain_token):
        if token:
            value = value.replace(token, "[redacted]")
    return value
