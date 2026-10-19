"""Loopback-only TV control for Mission Control through the existing tv-mcp.

Mission Control never talks to the Samsung TV directly and never reads the TV
token. It calls tv-mcp on 127.0.0.1:8065/mcp with a fixed allowlist:

    on      -> tv_on
    off     -> tv_off
    input   -> tv_input(input_name)          livetv | cable | dashboard | soundbar
    resync  -> tv_set_input_state(input_name) state only, no keys pressed
    status  -> tv_status                      read-only

Only one key-pressing request runs at a time; a second one gets TV_BUSY.
"""
import json
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit

DEFAULT_URL = "http://127.0.0.1:8065/mcp"
INPUTS = ("livetv", "cable", "dashboard", "soundbar")
ACTION_TIMEOUT_S = 75.0
STATUS_TIMEOUT_S = 6.0
PROTOCOL_VERSION = "2024-11-05"
ALLOWED_TOOLS = frozenset({"tv_on", "tv_off", "tv_input", "tv_set_input_state", "tv_status"})
RESULT_KEYS = (
    "ok", "error", "note", "hint", "woke", "woke_from_off", "method", "from", "to",
    "key_hdmi_presses", "input", "target", "PowerState", "PowerState_before",
    "remembered_input",
)


class TvUnavailable(Exception):
    pass


def _loopback_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
        raise TvUnavailable()
    if parsed.port != 8065 or parsed.path != "/mcp":
        raise TvUnavailable()
    return url


def urllib_transport(url, payload, headers, timeout_s, method="POST"):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code, {}, b""


def _decode(body, want_id=None):
    if not body:
        return None
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    if text.startswith("{"):
        return json.loads(text)
    found = None
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        message = json.loads(payload)
        if want_id is None or (isinstance(message, dict) and message.get("id") == want_id):
            found = message
    if found is None:
        raise TvUnavailable()
    return found


def unwrap_tool_result(message):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "error" in message:
        raise TvUnavailable()
    result = message.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        raise TvUnavailable()
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and set(structured) == {"result"}:
        structured = structured["result"]
    if isinstance(structured, dict):
        return structured
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                value = json.loads(item["text"])
                if isinstance(value, dict):
                    return value
    raise TvUnavailable()


class TvMcp:
    """Minimal JSON-RPC client for the loopback tv-mcp only."""

    def __init__(self, url=DEFAULT_URL, transport=urllib_transport):
        self.url = _loopback_url(url)
        self.transport = transport

    def call_tool(self, name, arguments, timeout_s):
        if name not in ALLOWED_TOOLS or not isinstance(arguments, dict):
            raise TvUnavailable()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        status, response_headers, body = self.transport(self.url, {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "syzygy-mission-control", "version": "0.1.0"},
            },
        }, headers, STATUS_TIMEOUT_S)
        message = _decode(body, 1)
        if status >= 300 or not isinstance(message, dict) or not isinstance(message.get("result"), dict):
            raise TvUnavailable()
        session = None
        for key, value in (response_headers or {}).items():
            if key.lower() == "mcp-session-id" and value:
                session = value
        if session:
            headers["Mcp-Session-Id"] = session
        headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        try:
            self.transport(self.url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, headers, STATUS_TIMEOUT_S)
            status, _, body = self.transport(self.url, {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }, headers, timeout_s)
            if status >= 300:
                raise TvUnavailable()
            return unwrap_tool_result(_decode(body, 2))
        finally:
            if session:
                try:
                    self.transport(self.url, None, headers, STATUS_TIMEOUT_S, method="DELETE")
                except Exception:
                    pass


def _safe_result(result):
    return {key: result[key] for key in RESULT_KEYS if key in result}


def _power(value):
    if value == "on":
        return "on"
    if value == "standby":
        return "standby"
    return "off_or_unreachable"


class TvControl:
    def __init__(self, client=None):
        self.client = client if client is not None else TvMcp()
        self._lock = threading.Lock()
        self._last = None

    def status(self):
        if self._lock.locked():
            view = dict(self._last or {"available": True, "power": "unknown", "input": None})
            view.update({"busy": True, "inputs": list(INPUTS)})
            return view
        try:
            result = self.client.call_tool("tv_status", {}, STATUS_TIMEOUT_S)
        except Exception:
            return {"available": False, "reason": "TV_MCP_UNAVAILABLE", "busy": False, "inputs": list(INPUTS)}
        remembered = result.get("remembered_input")
        view = {
            "available": True,
            "power": _power(result.get("PowerState")),
            "input": remembered if remembered in INPUTS else None,
            "busy": bool(result.get("busy")),
            "inputs": list(INPUTS),
        }
        self._last = {key: view[key] for key in ("available", "power", "input")}
        return view

    def act(self, action, input_name=None):
        action = action.strip().lower() if isinstance(action, str) else ""
        if action not in ("on", "off", "input", "resync"):
            return {"accepted": False, "reason": "INVALID_ACTION", "allowed": ["on", "off", "input", "resync"]}
        name = input_name.strip().lower() if isinstance(input_name, str) else ""
        if action in ("input", "resync") and name not in INPUTS:
            return {"accepted": False, "reason": "INVALID_INPUT", "allowed": list(INPUTS)}
        if action == "on":
            tool, arguments = "tv_on", {}
        elif action == "off":
            tool, arguments = "tv_off", {}
        elif action == "input":
            tool, arguments = "tv_input", {"input_name": name}
        else:
            tool, arguments = "tv_set_input_state", {"input_name": name}
        if not self._lock.acquire(blocking=False):
            return {"accepted": False, "reason": "TV_BUSY", "action": action}
        try:
            result = self.client.call_tool(tool, arguments, ACTION_TIMEOUT_S)
        except Exception:
            return {"accepted": False, "reason": "TV_MCP_UNAVAILABLE", "action": action}
        finally:
            self._lock.release()
        if not isinstance(result, dict):
            return {"accepted": False, "reason": "TV_MCP_UNAVAILABLE", "action": action}
        safe = _safe_result(result)
        if result.get("ok") is True:
            return {"accepted": True, "reason": "ACTION_ACCEPTED", "action": action, "input": name or None, "result": safe}
        reason = result.get("error") if isinstance(result.get("error"), str) else "TV_REJECTED"
        return {"accepted": False, "reason": reason[:64], "action": action, "result": safe}


def open_tv():
    return TvControl()