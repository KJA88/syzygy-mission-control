"""Localhost MCP for the shared SYZYGY capability registry.

There is no shell tool and no generic service caller. Bind stays on localhost.
"""
from __future__ import annotations

import json
import os
import uuid

from registry.authz import Grants, authorize
from registry.catalog import Catalog
from registry.dispatch import invoke
from registry.health import HealthBridge

# TV negotiates a 2026-07-28 offer down to 2025-11-25. Echoing 2026-07-28
# makes the portal stay on a protocol this server does not speak.
PROTOCOL_RESPONSE = {
    "2024-11-05": "2024-11-05",
    "2025-03-26": "2025-03-26",
    "2025-06-18": "2025-06-18",
    "2025-11-25": "2025-11-25",
    "2026-07-28": "2025-11-25",
}
SERVER_INFO = {"name": "syzygy-registry", "version": "1"}


def handle_rpc(catalog: Catalog, message, health: HealthBridge | None = None,
               headers=None, grants: Grants | None = None, write_log=None):
    if not isinstance(message, dict):
        return _error(None, -32600, "MALFORMED_PARAMETERS")
    method = message.get("method")
    rpc_id = message.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        protocol = params.get("protocolVersion")
        answered = PROTOCOL_RESPONSE.get(protocol)
        if answered is None:
            return _error(rpc_id, -32602, "MCP_HANDSHAKE_FAIL")
        return _result(rpc_id, {
            "protocolVersion": answered,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": dict(SERVER_INFO),
        })
    if method == "tools/list":
        return _result(rpc_id, {"tools": catalog.mcp_tools()})
    if method == "tools/call":
        params = dict(message.get("params") if isinstance(message.get("params"), dict) else {})
        params.pop("profile", None)
        name = params.get("name")
        arguments = dict(params.get("arguments") if isinstance(params.get("arguments"), dict) else {})
        arguments.pop("profile", None)
        authorization = authorize(headers, grants)
        payload = invoke(catalog, health, name, arguments, authorization, write_log)
        if payload.get("reason") == "UNKNOWN_CAPABILITY" and payload.get("capability") is None:
            return _error(rpc_id, -32601, "UNKNOWN_CAPABILITY")
        return _result(rpc_id, payload)
    return _error(rpc_id, -32601, "UNKNOWN_CAPABILITY")


def _result(rpc_id, result):
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _error(rpc_id, code, reason):
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": reason}}


def _session(headers, new=False):
    if not new and headers is not None:
        current = headers.get("Mcp-Session-Id")
        if isinstance(current, str) and current.strip():
            return current.strip()
    return uuid.uuid4().hex


def make_handler(catalog: Catalog, health: HealthBridge | None):
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path.rstrip("/") != "/mcp":
                self._send(404, b'{"error":"not found"}\n', "application/json")
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                message = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeError, ValueError):
                self._event(400, _error(None, -32700, "MALFORMED_PARAMETERS"))
                return
            if not isinstance(message, dict):
                message = {}
            response = handle_rpc(catalog, message, health, self.headers, Grants.from_env())
            session = _session(self.headers, new=message.get("method") == "initialize")
            if response is None:
                self._send(202, b"", "application/json", session)
                return
            self._event(200, response, session)

        def do_GET(self):
            self._idle(self.headers.get("Mcp-Session-Id"))

        def do_DELETE(self):
            self._idle(self.headers.get("Mcp-Session-Id"))

        def log_message(self, fmt, *args):
            return

        def _event(self, status, payload, session=None):
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self._send(status, b"event: message\r\ndata: " + encoded + b"\r\n\r\n", "text/event-stream", session)

        def _idle(self, session):
            if not session:
                body = json.dumps({
                    "jsonrpc": "2.0",
                    "id": "server-error",
                    "error": {"code": -32600, "message": "Bad Request: Missing session ID"},
                }, separators=(",", ":")).encode("utf-8")
                self._send(400, body, "application/json", uuid.uuid4().hex)
                return
            self._send(200, b":\r\n\r\n", "text/event-stream", session)

        def _send(self, status, body, content_type, session=None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            if session:
                self.send_header("mcp-session-id", session)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def bridge_from_env() -> HealthBridge:
    return HealthBridge(
        os.environ.get("HEALTH_WORKBOOK_URL", "http://127.0.0.1:5052"),
        os.environ.get("HEALTH_WORKBOOK_READ_TOKEN", ""),
        os.environ.get("HEALTH_WORKBOOK_MAINTAIN_TOKEN", ""),
    )


def main(argv=None):
    from http.server import ThreadingHTTPServer

    host = os.environ.get("SYZYGY_REGISTRY_HOST", "127.0.0.1")
    port = int(os.environ.get("SYZYGY_REGISTRY_PORT", "8076"))
    if host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("registry MCP binds to localhost only")
    catalog = Catalog.load()
    Grants.from_env()
    server = ThreadingHTTPServer((host, port), make_handler(catalog, bridge_from_env()))
    server.serve_forever()


if __name__ == "__main__":
    main()
