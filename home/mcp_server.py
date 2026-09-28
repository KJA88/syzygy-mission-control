"""Small local MCP for the shared Home Assistant adapter.

There is no generic call_service tool. Bind stays on localhost.
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROTOCOL = "2024-11-05"
SERVER_INFO = {"name": "syzygy-home-assistant", "version": "0.1.0"}

TOOLS = [
    {
        "name": "list_entities",
        "description": "List Home Assistant entities exposed to SYZYGY.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_entity",
        "description": "Read one exposed entity.",
        "inputSchema": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_sensor",
        "description": "Read one sensor or binary sensor.",
        "inputSchema": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "turn_on",
        "description": "Turn on an explicitly allowed switch or light.",
        "inputSchema": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "turn_off",
        "description": "Turn off an explicitly allowed switch or light.",
        "inputSchema": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "set_brightness",
        "description": "Set brightness on an explicitly allowed light that supports it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "brightness": {"type": "integer", "minimum": 0, "maximum": 255},
            },
            "required": ["entity_id", "brightness"],
            "additionalProperties": False,
        },
    },
]


def handle_rpc(adapter, message):
    """Return a JSON-RPC response dict, or None for a notification."""
    if not isinstance(message, dict):
        return _error(None, -32600, "MALFORMED_PARAMETERS")
    method = message.get("method")
    rpc_id = message.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if params.get("protocolVersion") != PROTOCOL:
            return _error(rpc_id, -32602, "MCP_HANDSHAKE_FAIL")
        return _result(rpc_id, {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": dict(SERVER_INFO),
        })
    if method == "tools/list":
        return _result(rpc_id, {"tools": TOOLS})
    if method == "tools/call":
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        name = params.get("name")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        if name == "call_service":
            return _error(rpc_id, -32601, "DOMAIN_REJECTED")
        payload = _call_tool(adapter, name, arguments)
        if payload.get("accepted") is False and payload.get("reason") == "UNKNOWN_TOOL":
            return _error(rpc_id, -32601, "UNKNOWN_TOOL")
        return _result(rpc_id, payload)
    return _error(rpc_id, -32601, "UNKNOWN_TOOL")


def _call_tool(adapter, name, arguments):
    if name == "list_entities":
        view = adapter.entity_view()
        return {
            "accepted": view.get("available") is True,
            "reason": view.get("reason"),
            "entities": view.get("entities") or [],
        }
    entity_id = arguments.get("entity_id")
    if name == "get_entity":
        return _read(adapter, entity_id, None)
    if name == "get_sensor":
        return _read(adapter, entity_id, {"sensor", "binary_sensor"})
    if name == "turn_on":
        return adapter.act(entity_id, "turn_on")
    if name == "turn_off":
        return adapter.act(entity_id, "turn_off")
    if name == "set_brightness":
        return adapter.act(entity_id, "set_brightness", arguments.get("brightness"))
    return {"accepted": False, "reason": "UNKNOWN_TOOL"}


def _read(adapter, entity_id, domains):
    from home.adapter import entity_id_of

    if entity_id_of(entity_id) is None:
        return {"accepted": False, "reason": "MALFORMED_PARAMETERS"}
    view = adapter.entity_view()
    if not view.get("available"):
        return {"accepted": False, "reason": view.get("reason") or "HA_UNAVAILABLE"}
    for entity in view["entities"]:
        if entity["entity_id"] == entity_id:
            if domains is not None and entity["domain"] not in domains:
                return {"accepted": False, "reason": "DOMAIN_REJECTED"}
            return {"accepted": True, "reason": None, "entity": entity}
    return {"accepted": False, "reason": "UNKNOWN_ENTITY"}


def _result(rpc_id, result):
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _error(rpc_id, code, reason):
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": reason}}


def make_handler(adapter):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path.rstrip("/") != "/mcp":
                self._send(404, b'{"error":"not found"}\n')
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                message = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeError, ValueError):
                self._send(400, json.dumps(_error(None, -32700, "MALFORMED_PARAMETERS")).encode("utf-8"))
                return
            response = handle_rpc(adapter, message)
            if response is None:
                self._send(202, b"")
                return
            self._send(200, json.dumps(response).encode("utf-8"))

        def log_message(self, fmt, *args):
            return

        def _send(self, status, body):
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def load_adapter():
    from pathlib import Path

    from guardian.config import load_config
    from home.adapter import HomeAssistant, policy_from_config

    root = Path(__file__).resolve().parents[1]
    raw = {}
    try:
        loaded = load_config(root / "config" / "services.yaml")
        if isinstance(loaded.get("home_assistant"), dict):
            raw = loaded["home_assistant"]
    except (OSError, ValueError, KeyError, TypeError):
        raw = {}
    policy = policy_from_config(raw)
    return HomeAssistant(policy, timeout=policy["timeout"])


def main(argv=None):
    host = os.environ.get("HA_MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("HA_MCP_PORT", "8095"))
    if host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("home MCP binds to localhost only")
    server = ThreadingHTTPServer((host, port), make_handler(load_adapter()))
    server.serve_forever()


if __name__ == "__main__":
    main()
