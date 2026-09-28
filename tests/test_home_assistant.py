"""Offline Home Assistant adapter, MCP, and Mission Control routes."""
import importlib.util
import io
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from guardian.aggregator import Guardian, build_snapshot
from guardian.config import load_config
from guardian.model import fact, stamp
from guardian.probes import mcp
from home.adapter import HomeAssistant, devices_from_entities, policy_from_config
from home.mcp_server import TOOLS, handle_rpc, load_adapter

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "super-secret-token"


class Body:
    def __init__(self, payload, status=200):
        self.status = status
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self._body = raw

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class Opener:
    def __init__(self, states=None, error=None, post_status=200, raw=None):
        self.states = [] if states is None else states
        self.error = error
        self.post_status = post_status
        self.raw = raw
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request.get_method(), request.full_url, request.data, timeout))
        if self.error is not None:
            raise self.error
        if request.get_method() == "GET":
            return Body(self.raw if self.raw is not None else self.states)
        return Body([], self.post_status)


def entity(entity_id, state="on", **attributes):
    return {
        "entity_id": entity_id,
        "state": state,
        "last_changed": "2026-09-28T00:00:00+00:00",
        "attributes": attributes,
    }


def policy(allow=None, **raw):
    env = {"HA_BASE_URL": "http://ha.example:8123", "HA_TOKEN": TOKEN}
    raw.setdefault("write_allow", ["switch.porch", "light.desk"] if allow is None else allow)
    return policy_from_config(raw, env)


def adapter(states, allow=None, error=None, post_status=200, **raw):
    return HomeAssistant(policy(allow, **raw), opener=Opener(states, error, post_status))


def posts(client):
    return [url for method, url, _data, _timeout in client.opener.calls if method == "POST"]


class HomeAssistantTests(unittest.TestCase):
    def test_normalizes_dynamic_entities_without_a_fixed_inventory(self):
        first = adapter([
            entity("sensor.temp", "21.5", friendly_name="Temp", unit_of_measurement="°C", device_class="temperature"),
            "skip-me",
            {"entity_id": "Not A Sensor", "state": "1"},
            entity("binary_sensor.door", "off", friendly_name="Door", device_class="door"),
        ])
        view = first.entity_view()
        self.assertTrue(view["available"])
        self.assertEqual([item["entity_id"] for item in view["entities"]], ["sensor.temp", "binary_sensor.door"])
        sensor = view["entities"][0]
        self.assertEqual(sensor["provider"], "home_assistant")
        self.assertEqual(sensor["unit"], "°C")
        self.assertTrue(sensor["readable"])
        self.assertFalse(sensor["writable"])
        self.assertEqual(sensor["capabilities"], ["read"])
        second = adapter([
            entity("sensor.temp", "21.5", friendly_name="Temp"),
            entity("sensor.added_later", "3", friendly_name="Added"),
        ])
        names = [item["entity_id"] for item in second.entity_view()["entities"]]
        self.assertEqual(names, ["sensor.temp", "sensor.added_later"])
        self.assertEqual(second.entity_view()["counts"]["sensor"], 2)

    def test_include_and_exclude_filter_discovery(self):
        states = [
            entity("sensor.keep", "1", friendly_name="Keep"),
            entity("sensor.hide", "2", friendly_name="Hide"),
        ]
        kept = adapter(states, include=["sensor.keep"]).entity_view()
        self.assertEqual([item["entity_id"] for item in kept["entities"]], ["sensor.keep"])
        hidden = adapter(states, exclude=["sensor.hide"]).entity_view()
        self.assertEqual([item["entity_id"] for item in hidden["entities"]], ["sensor.keep"])

    def test_only_allowed_switches_and_lights_are_writable(self):
        states = [
            entity("switch.porch", "off", friendly_name="Porch"),
            entity("switch.other", "off", friendly_name="Other"),
            entity("light.desk", "on", friendly_name="Desk", supported_color_modes=["brightness"]),
            entity("light.plain", "on", friendly_name="Plain"),
            entity("sensor.temp", "1", friendly_name="Temp"),
        ]
        client = adapter(states)
        by_id = {item["entity_id"]: item for item in client.entity_view()["entities"]}
        self.assertEqual(by_id["switch.porch"]["capabilities"], ["read", "turn_on", "turn_off"])
        self.assertFalse(by_id["switch.other"]["writable"])
        self.assertIn("brightness", by_id["light.desk"]["capabilities"])
        self.assertNotIn("brightness", by_id["light.plain"]["capabilities"])
        self.assertEqual(client.act("switch.porch", "turn_on")["reason"], "ACTION_ACCEPTED")
        self.assertTrue(any(url.endswith("/api/services/switch/turn_on") for url in posts(client)))
        denied = adapter(states)
        self.assertEqual(denied.act("switch.other", "turn_on")["reason"], "POLICY_DENIED")
        self.assertEqual(posts(denied), [])
        empty = adapter(states, allow=[]).act("switch.porch", "turn_off")
        self.assertEqual(empty["reason"], "POLICY_DENIED")
        sensor = adapter(states, allow=["sensor.temp"]).act("sensor.temp", "turn_on")
        self.assertEqual(sensor["reason"], "POLICY_DENIED")
        plain = adapter(states, allow=["light.plain"]).act("light.plain", "set_brightness", 10)
        self.assertEqual(plain["reason"], "CAPABILITY_UNAVAILABLE")
        bright = adapter(states)
        self.assertEqual(bright.act("light.desk", "set_brightness", 40)["reason"], "ACTION_ACCEPTED")
        sent = [call[2] for call in bright.opener.calls if call[0] == "POST"]
        self.assertEqual(json.loads(sent[0]), {"entity_id": "light.desk", "brightness": 40})
        for bad in (True, 1.5, -1, 256, "40"):
            self.assertEqual(adapter(states).act("light.desk", "set_brightness", bad)["reason"], "MALFORMED_PARAMETERS")
        offline = adapter([entity("switch.porch", "unavailable", friendly_name="Porch")])
        self.assertEqual(offline.act("switch.porch", "turn_on")["reason"], "HA_UNAVAILABLE")
        self.assertEqual(posts(offline), [])

    def test_high_risk_domains_are_rejected_before_a_service_call(self):
        states = [
            entity("lock.front", "locked"),
            entity("alarm_control_panel.home", "armed_away"),
            entity("cover.garage", "closed"),
            entity("climate.hall", "heat"),
            entity("water_heater.tank", "eco"),
            entity("script.goodnight", "off"),
            entity("automation.night", "on"),
            entity("scene.movie", "unknown"),
            entity("media_player.tv", "off"),
        ]
        allow = [item["entity_id"] for item in states]
        client = adapter(states, allow=allow, read_domains=["sensor", "binary_sensor", "switch", "light", "lock"])
        self.assertEqual(client.entity_view()["entities"], [])
        for item in states:
            result = client.act(item["entity_id"], "turn_on")
            self.assertEqual(result["reason"], "DOMAIN_REJECTED", item["entity_id"])
        self.assertEqual(posts(client), [])

    def test_transport_failures_use_fixed_reasons_and_drop_secrets(self):
        secret_body = io.BytesIO(b"Bearer " + TOKEN.encode("utf-8"))
        auth = adapter([], error=urllib.error.HTTPError(
            "http://ha.example/api/states", 401, "denied", {}, secret_body))
        view = auth.entity_view()
        self.assertEqual(view["reason"], "HA_AUTH")
        self.assertNotIn(TOKEN, json.dumps(view))
        timed = adapter([], error=TimeoutError("Bearer " + TOKEN))
        self.assertEqual(timed.status()["reason"], "HA_TIMEOUT")
        self.assertNotIn(TOKEN, json.dumps(timed.status()))
        down = adapter([], error=urllib.error.URLError("connection refused " + TOKEN))
        self.assertEqual(down.entity_view()["reason"], "HA_UNAVAILABLE")
        self.assertNotIn(TOKEN, json.dumps(down.entity_view()))
        malformed = HomeAssistant(policy(), opener=Opener(raw=b'{"token":"' + TOKEN.encode("utf-8") + b'"}'))
        self.assertEqual(malformed.entity_view()["reason"], "HA_MALFORMED")
        self.assertNotIn(TOKEN, json.dumps(malformed.entity_view()))
        missing = HomeAssistant(policy_from_config({"write_allow": []}, {}), opener=Opener([]))
        self.assertEqual(missing.entity_view()["reason"], "HA_UNCONFIGURED")
        self.assertEqual(missing.opener.calls, [])
        no_token = HomeAssistant(policy_from_config({"token": TOKEN}, {"HA_BASE_URL": "http://ha.example"}), opener=Opener([]))
        self.assertEqual(no_token.entity_view()["reason"], "HA_AUTH")
        self.assertEqual(no_token.opener.calls, [])
        self.assertNotIn(TOKEN, json.dumps(no_token.entity_view()))

    def test_yaml_cannot_carry_the_token_and_devices_follow_the_view(self):
        loaded = policy_from_config({"token": TOKEN, "write_allow": []}, {"HA_BASE_URL": "http://ha.example", "HA_TOKEN": ""})
        self.assertEqual(loaded["token"], "")
        text = (ROOT / "config" / "services.yaml").read_text(encoding="utf-8")
        self.assertIn("write_allow: []", text)
        self.assertNotRegex(text, r"(?m)^HA_TOKEN\s*[:=]")
        self.assertNotIn(TOKEN, text)
        example = (ROOT / "config" / "home-assistant.env.example").read_text(encoding="utf-8")
        self.assertIn("HA_TOKEN=", example)
        self.assertNotIn(TOKEN, example)
        devices = devices_from_entities(adapter([
            entity("sensor.temp", "1", friendly_name="Temp", area="Hall"),
        ]).entity_view()["entities"])
        self.assertEqual(devices[0]["id"], "home:sensor.temp")
        self.assertEqual(devices[0]["provider"], "home_assistant")
        self.assertEqual(devices[0]["health"], "online")

    def test_mcp_tools_are_narrow_and_share_the_adapter(self):
        names = [tool["name"] for tool in TOOLS]
        self.assertEqual(names, [
            "list_entities", "get_entity", "get_sensor", "turn_on", "turn_off", "set_brightness",
        ])
        self.assertNotIn("call_service", names)
        for tool in TOOLS:
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
        client = adapter([
            entity("sensor.temp", "21", friendly_name="Temp"),
            entity("switch.porch", "off", friendly_name="Porch"),
        ])
        opened = handle_rpc(client, {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "guardian", "version": "1"}},
        })
        self.assertEqual(opened["id"], 7)
        self.assertEqual(opened["result"]["protocolVersion"], "2024-11-05")
        self.assertIsInstance(opened["result"]["capabilities"], dict)
        self.assertEqual(opened["result"]["serverInfo"]["name"], "syzygy-home-assistant")
        self.assertIsNone(handle_rpc(client, {"method": "notifications/initialized"}))
        listed = handle_rpc(client, {"jsonrpc": "2.0", "id": 8, "method": "tools/list"})
        self.assertEqual([tool["name"] for tool in listed["result"]["tools"]], names)
        reading = handle_rpc(client, {
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {"name": "get_sensor", "arguments": {"entity_id": "sensor.temp"}},
        })
        self.assertEqual(reading["result"]["entity"]["state"], "21")
        rejected = handle_rpc(client, {
            "jsonrpc": "2.0", "id": 10, "method": "tools/call",
            "params": {"name": "get_sensor", "arguments": {"entity_id": "switch.porch"}},
        })
        self.assertEqual(rejected["result"]["reason"], "DOMAIN_REJECTED")
        blocked = handle_rpc(client, {
            "jsonrpc": "2.0", "id": 11, "method": "tools/call",
            "params": {"name": "call_service", "arguments": {"domain": "lock", "service": "unlock"}},
        })
        self.assertEqual(blocked["error"]["message"], "DOMAIN_REJECTED")
        turned = handle_rpc(client, {
            "jsonrpc": "2.0", "id": 12, "method": "tools/call",
            "params": {"name": "turn_on", "arguments": {"entity_id": "switch.porch"}},
        })
        self.assertEqual(turned["result"]["reason"], "ACTION_ACCEPTED")
        self.assertNotIn(TOKEN, json.dumps(turned))

    def test_mission_control_home_routes_keep_the_rest_of_the_cockpit(self):
        page = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        self.assertIn('data-view="home"', page)
        self.assertIn('id="view-home"', page)
        self.assertIn("id=\"operational\"", page)
        self.assertIn("/api/home/entities", script)
        self.assertNotIn(TOKEN, script)
        self.assertNotIn("HA_TOKEN", script)
        self.assertNotIn("192.168.1.17", script)
        spec = importlib.util.spec_from_file_location("mc_home_server", ROOT / "ui" / "server.py")
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)

        class FakeVision:
            def camera_view(self):
                return {"available": True, "cameras": [{"id": "dock", "name": "Dock", "capabilities": ["stream"]}]}

        class DownHome:
            def entity_view(self):
                raise RuntimeError("Bearer " + TOKEN)

            def status(self):
                return {"available": False, "reason": "HA_UNAVAILABLE", "ui_url": None, "counts": {}}

            def act(self, *_args):
                return {"accepted": False, "reason": "HA_UNAVAILABLE"}

        handler = server.make_handler(ROOT / "state", ROOT / "ui", 120, 20, owner=None, vision=FakeVision(), home=DownHome())
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/devices" % httpd.server_port, timeout=2) as response:
                devices = json.loads(response.read().decode("utf-8"))
            self.assertEqual(devices["devices"][0]["provider_id"], "dock")
            self.assertEqual(devices["devices"][1]["id"], "arm:roarm-1")
            self.assertNotIn(TOKEN, json.dumps(devices))
            body = json.dumps({"entity_id": "switch.porch", "action": "turn_on"}).encode("utf-8")
            request = urllib.request.Request(
                "http://127.0.0.1:%d/api/home/action" % httpd.server_port,
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                self.assertEqual(response.status, 200)
                action = json.loads(response.read().decode("utf-8"))
            self.assertEqual(action["reason"], "HA_UNAVAILABLE")
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_policy_timeout_is_shared_by_the_cockpit_and_mcp(self):
        chosen = policy_from_config({"timeout_s": 2.5}, {"HA_BASE_URL": "http://ha.example", "HA_TOKEN": TOKEN})
        self.assertEqual(HomeAssistant(chosen).timeout, 2.5)
        self.assertEqual(HomeAssistant(chosen, timeout=chosen["timeout"]).timeout, 2.5)
        with patch("guardian.config.load_config", return_value={"home_assistant": {"timeout_s": 2.5}}):
            self.assertEqual(load_adapter().timeout, 2.5)

    def test_optional_ha_outage_stays_off_the_required_rollup(self):
        cfg = load_config(ROOT / "config" / "services.yaml")
        home = next(service for service in cfg["services"] if service["id"] == "home-assistant-mcp")
        self.assertIs(home["required"], False)
        self.assertEqual(home["local_url"], "http://127.0.0.1:8095")
        self.assertEqual(home["mcp_path"], "/mcp")
        self.assertEqual(home["expected_tool_names"], [
            "list_entities", "get_entity", "get_sensor", "turn_on", "turn_off", "set_brightness",
        ])
        self.assertIs(home["probe"]["tool_calls_allowed"], False)
        self.assertFalse(home["probe"].get("functional_probe_enabled", False))
        self.assertEqual(home.get("auth_probe", "off"), "off")
        self.assertNotIn("public_url", home)
        self.assertNotIn("cloudflared_unit", home)
        now = 1800000000
        evidence = {}
        for node in cfg["nodes"]:
            evidence[node["id"]] = {"observed_at": stamp(now), "services": [], "paths": []}
        for service in cfg["services"]:
            if not service["required"]:
                continue
            layers = {
                "local_service": fact("green", True, now, "t"),
                "local_port": fact("green", True, now, "t"),
            }
            if "health_url" in service:
                layers["local_health"] = fact("green", True, now, "t")
            if "probe" in service:
                layers["local_mcp"] = fact("green", True, now, "t")
            for camera in service.get("cameras", []):
                layers["camera/" + camera["id"]] = fact("green", camera["required"], now, "t")
            evidence[service["host"]]["services"].append({"id": service["id"], "layers": layers})
        hidden = build_snapshot(cfg, evidence, {}, now, "t")
        observed = next(service for service in hidden["services"] if service["id"] == "home-assistant-mcp")
        self.assertFalse(observed["required"])
        self.assertEqual(observed["status"], "unknown")
        self.assertEqual(hidden["system"]["status"], "green")
        self.assertEqual(hidden["guardian"]["status"], "green")
        evidence["pi"]["services"].append({
            "id": "home-assistant-mcp",
            "layers": {"local_mcp": fact("red", True, now, "t", "MCP_HANDSHAKE_FAIL")},
        })
        down = build_snapshot(cfg, evidence, {}, now, "t")
        failed = next(service for service in down["services"] if service["id"] == "home-assistant-mcp")
        self.assertEqual(failed["status"], "red")
        self.assertFalse(failed["required"])
        self.assertEqual(down["system"]["status"], "green")
        self.assertEqual(down["guardian"]["status"], "green")
        methods = []

        def transport(url, timeout, payload, headers, rpc_id):
            methods.append(payload["method"])
            if payload["method"] == "notifications/initialized":
                return None, {}
            if payload["method"] == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "syzygy-home-assistant", "version": "0.1.0"},
                }
            else:
                result = {"tools": [{"name": name} for name in home["expected_tool_names"]]}
            return {"jsonrpc": "2.0", "id": payload["id"], "result": result}, {}

        health, catalog = mcp(home, "http://127.0.0.1:8095/mcp", False, "t", transport)
        self.assertEqual(methods, ["initialize", "notifications/initialized", "tools/list"])
        self.assertNotIn("tools/call", methods)
        self.assertEqual(health["status"], "green")
        self.assertEqual(catalog["status"], "green")
        quiet = fact("green", False, now, "t")
        with patch("guardian.aggregator.agent_evidence", return_value=None), \
                patch("guardian.aggregator.mcp", return_value=(quiet, quiet)) as public, \
                patch("guardian.aggregator.auth_mcp", return_value=quiet) as auth, \
                patch("guardian.aggregator.probe_roarm", return_value={"status": "green"}):
            Guardian(cfg).tick()
        probed = [call.args[0]["id"] for call in public.call_args_list]
        self.assertNotIn("home-assistant-mcp", probed)
        self.assertNotIn("home-assistant-mcp", [call.args[0]["id"] for call in auth.call_args_list])


if __name__ == "__main__":
    unittest.main()
