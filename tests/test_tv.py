"""Mission Control TV card: loopback tv-mcp client, allowlist, busy lock, routes, UI."""
import importlib.util
import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from home.tv import INPUTS, TvControl, TvMcp, TvUnavailable

ROOT = Path(__file__).resolve().parent.parent


class FakeClient:
    def __init__(self, result=None, error=None, gate=None):
        self.calls = []
        self.result = result if result is not None else {"ok": True}
        self.error = error
        self.gate = gate

    def call_tool(self, name, arguments, timeout_s):
        self.calls.append((name, arguments, timeout_s))
        if self.gate is not None:
            self.gate.wait(2)
        if self.error is not None:
            raise self.error
        return dict(self.result)


def sse(message):
    return ("event: message\r\ndata: " + json.dumps(message) + "\r\n\r\n").encode()


class FakeTransport:
    def __init__(self, tool_result, use_sse=True):
        self.calls = []
        self.tool_result = tool_result
        self.use_sse = use_sse

    def __call__(self, url, payload, headers, timeout_s, method="POST"):
        self.calls.append((method, payload and payload.get("method"), dict(headers), timeout_s))
        if method == "DELETE":
            return 200, {}, b""
        if payload["method"] == "initialize":
            body = {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2024-11-05", "capabilities": {}}}
            return 200, {"mcp-session-id": "s-1"}, sse(body) if self.use_sse else json.dumps(body).encode()
        if payload["method"] == "notifications/initialized":
            return 202, {}, b""
        body = {"jsonrpc": "2.0", "id": 2, "result": {
            "content": [{"type": "text", "text": json.dumps(self.tool_result)}],
            "structuredContent": self.tool_result, "isError": False}}
        return 200, {}, sse(body) if self.use_sse else json.dumps(body).encode()


class TvMcpTests(unittest.TestCase):
    def test_only_loopback_8065_mcp_is_accepted(self):
        for url in ("http://192.168.1.18:8065/mcp", "https://127.0.0.1:8065/mcp",
                    "http://127.0.0.1:8010/mcp", "http://127.0.0.1:8065/other",
                    "https://tv.syzygylab.net/mcp"):
            with self.subTest(url=url), self.assertRaises(TvUnavailable):
                TvMcp(url)
        TvMcp("http://127.0.0.1:8065/mcp")

    def test_tool_allowlist(self):
        client = TvMcp(transport=FakeTransport({"ok": True}))
        for name in ("tv_wall_page", "tv_dashboard", "tv_cable", "shell"):
            with self.subTest(name=name), self.assertRaises(TvUnavailable):
                client.call_tool(name, {}, 1)

    def test_session_flow_sse_and_json(self):
        for use_sse in (True, False):
            with self.subTest(sse=use_sse):
                transport = FakeTransport({"ok": True, "PowerState": "on"}, use_sse)
                result = TvMcp(transport=transport).call_tool("tv_status", {}, 6)
                self.assertEqual(result["PowerState"], "on")
                self.assertEqual([c[1] for c in transport.calls],
                                 ["initialize", "notifications/initialized", "tools/call", None])
                self.assertEqual(transport.calls[-1][0], "DELETE")
                self.assertEqual(transport.calls[2][2]["Mcp-Session-Id"], "s-1")


class TvControlTests(unittest.TestCase):
    def test_actions_map_to_fixed_tools(self):
        cases = [
            (("on", None), ("tv_on", {})),
            (("off", None), ("tv_off", {})),
            (("input", "soundbar"), ("tv_input", {"input_name": "soundbar"})),
            (("INPUT", " LiveTV "), ("tv_input", {"input_name": "livetv"})),
            (("resync", "cable"), ("tv_set_input_state", {"input_name": "cable"})),
        ]
        for (action, name), (tool, args) in cases:
            with self.subTest(action=action, name=name):
                client = FakeClient()
                result = TvControl(client).act(action, name)
                self.assertTrue(result["accepted"])
                self.assertEqual(client.calls[0][:2], (tool, args))
        self.assertEqual(client.calls[0][2], 75.0)

    def test_invalid_action_and_input_send_nothing(self):
        client = FakeClient()
        tv = TvControl(client)
        self.assertEqual(tv.act("reboot")["reason"], "INVALID_ACTION")
        self.assertEqual(tv.act(None)["reason"], "INVALID_ACTION")
        self.assertEqual(tv.act("input", "hdmi4")["reason"], "INVALID_INPUT")
        self.assertEqual(tv.act("input")["reason"], "INVALID_INPUT")
        self.assertEqual(tv.act("resync", 3)["reason"], "INVALID_INPUT")
        self.assertEqual(client.calls, [])
        self.assertEqual(set(INPUTS), {"livetv", "cable", "dashboard", "soundbar"})

    def test_second_request_while_busy_is_tv_busy(self):
        gate = threading.Event()
        client = FakeClient(gate=gate)
        tv = TvControl(client)
        first = {}
        worker = threading.Thread(target=lambda: first.update(tv.act("on")))
        worker.start()
        for _ in range(200):
            if client.calls:
                break
            threading.Event().wait(0.01)
        self.assertEqual(tv.act("input", "cable")["reason"], "TV_BUSY")
        busy_status = tv.status()
        self.assertTrue(busy_status["busy"])
        gate.set()
        worker.join(3)
        self.assertTrue(first["accepted"])
        self.assertEqual([c[0] for c in client.calls], ["tv_on"])

    def test_failures_do_not_leak_details(self):
        tv = TvControl(FakeClient(error=RuntimeError("secret-token-value")))
        result = tv.act("off")
        self.assertEqual(result["reason"], "TV_MCP_UNAVAILABLE")
        self.assertNotIn("secret-token-value", json.dumps(result))
        status = tv.status()
        self.assertFalse(status["available"])
        rejected = TvControl(FakeClient(result={"ok": False, "error": "TV_DID_NOT_WAKE",
                                                "detail": "x", "tv": "192.168.1.221"})).act("on")
        self.assertFalse(rejected["accepted"])
        self.assertEqual(rejected["reason"], "TV_DID_NOT_WAKE")
        self.assertNotIn("192.168.1.221", json.dumps(rejected))
        self.assertNotIn("detail", rejected["result"])

    def test_status_normalizes_power_and_input(self):
        for raw, power in (("on", "on"), ("standby", "standby"), (None, "off_or_unreachable")):
            with self.subTest(raw=raw):
                client = FakeClient(result={"ok": True, "PowerState": raw, "remembered_input": "cable", "busy": False})
                view = TvControl(client).status()
                self.assertEqual((view["available"], view["power"], view["input"]), (True, power, "cable"))
                self.assertEqual(client.calls[0][:2], ("tv_status", {}))
        view = TvControl(FakeClient(result={"ok": True, "remembered_input": "weird"})).status()
        self.assertIsNone(view["input"])


class TvRouteTests(unittest.TestCase):
    def _server(self, tv):
        spec = importlib.util.spec_from_file_location("mc_tv_server", ROOT / "ui" / "server.py")
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)
        handler = server.make_handler(ROOT / "state", ROOT / "ui", 120, 20, owner=None, tv=tv)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd

    def _get(self, port, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=3) as r:
            return json.loads(r.read())

    def _post(self, port, path, body):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read())

    def test_routes_use_tv_control(self):
        client = FakeClient(result={"ok": True, "PowerState": "on", "remembered_input": "dashboard"})
        httpd = self._server(TvControl(client))
        try:
            port = httpd.server_port
            self.assertEqual(self._get(port, "/api/tv/status")["input"], "dashboard")
            self.assertTrue(self._post(port, "/api/tv/action", {"action": "input", "input": "livetv"})["accepted"])
            self.assertEqual(self._post(port, "/api/tv/action", {"action": "wall"})["reason"], "INVALID_ACTION")
            self.assertEqual([c[0] for c in client.calls], ["tv_status", "tv_input"])
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_routes_without_tv(self):
        httpd = self._server(None)
        try:
            port = httpd.server_port
            self.assertEqual(self._get(port, "/api/tv/status")["reason"], "TV_UNCONFIGURED")
            self.assertEqual(self._post(port, "/api/tv/action", {"action": "on"})["reason"], "TV_UNCONFIGURED")
        finally:
            httpd.shutdown()
            httpd.server_close()


class TvUiTests(unittest.TestCase):
    def test_home_tab_has_tv_card(self):
        page = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        home = page.split('id="view-home"', 1)[1].split("</section>", 1)[0]
        self.assertIn('id="tv-card"', home)
        for marker in ('data-tv-action="on"', 'data-tv-action="off"', 'data-tv-input="livetv"',
                       'data-tv-input="cable"', 'data-tv-input="dashboard"', 'data-tv-input="soundbar"',
                       'id="tv-resync-send"', 'id="tv-status"'):
            self.assertIn(marker, home)
        self.assertLess(home.index('id="tv-card"'), home.index('id="home-controls"'))
        self.assertIn('fetch("/api/tv/status", { cache: "no-store" })', script)
        self.assertIn('fetch("/api/tv/action", {', script)
        self.assertNotIn("192.168.1.221", script)
        self.assertNotIn("8065", script)
        self.assertNotIn("samsung-tv-token", script)

    def test_service_worker_cache_bumped(self):
        text = (ROOT / "ui" / "sw.js").read_text(encoding="utf-8")
        self.assertIn('const CACHE = "syzygy-shell-v5";', text)


if __name__ == "__main__":
    unittest.main()