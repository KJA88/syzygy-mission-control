"""Capability registry discovery, permissions, and health proxy."""

import json
import unittest

from registry.authz import Authorization, Grants
from registry.catalog import Catalog, CatalogError
from registry.dispatch import invoke
from registry.health import HealthBridge
from registry.mcp_server import handle_rpc

REQUIRED_SYSTEMS = {
    "health-workbook",
    "mission-control",
    "roarm",
    "vision",
    "fitbit",
    "polar",
    "pi",
    "jetson",
}


class FakeHealth:
    def __init__(self):
        self.calls = []

    def transport(self, method, url, token, body):
        self.calls.append((method, url, token, body))
        if "/v1/audit" in url:
            return 200, {"entries": [{"sheet": "Notes", "action": "append"}], "limit": 20}
        if method == "POST" and url.endswith("/v1/notes"):
            return 201, {"result": "appended", "sheet": "Notes"}
        if method == "GET" and "/v1/sheets/weight-trend" in url:
            return 200, {"sheet": "Weight Trend", "rows": []}
        return 200, {"ok": True}


def bridge(fake):
    return HealthBridge("http://127.0.0.1:5052", "read-token", "maintain-token", fake.transport)


def call(catalog, health, name, arguments=None, profile="discover"):
    return invoke(catalog, health, name, arguments or {}, Authorization.resolved(profile))


class RegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog.load()

    def test_discovery_lists_the_shared_subsystems(self):
        systems = call(self.catalog, None, "syzygy.systems")["result"]["systems"]
        ids = {item["id"] for item in systems}
        self.assertTrue(REQUIRED_SYSTEMS.issubset(ids))
        health = next(item for item in systems if item["id"] == "health-workbook")
        self.assertEqual(health["capabilities"], ["health.read", "health.write", "health.audit"])
        self.assertIn("health_workbook/README.md", health["docs"])
        tools = handle_rpc(self.catalog, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = [item["name"] for item in tools["result"]["tools"]]
        self.assertIn("health.read", names)
        self.assertIn("roarm.motion", names)
        self.assertIn("camera.list", names)
        self.assertIn("pi.status", names)
        self.assertIn("jetson.status", names)
        self.assertFalse(any("shell" in name or "xlsx" in name or "delete" in name for name in names))

    def test_profile_permissions_and_unknown_capability(self):
        visible = call(self.catalog, None, "syzygy.tools")["result"]
        self.assertEqual(visible["profile"], "discover")
        write = next(item for item in visible["tools"] if item["name"] == "health.write")
        self.assertFalse(write["executable"])
        motion = next(item for item in visible["tools"] if item["name"] == "roarm.motion")
        self.assertFalse(motion["executable"])
        denied = call(self.catalog, None, "health.write", {
            "sheet": "notes",
            "values": {"note": "x"},
            "source": "registry",
            "updated_by": "tester",
            "recorded_at": "2026-10-04T00:00:00Z",
        })
        self.assertEqual(denied["reason"], "PERMISSION_DENIED")
        owner = call(self.catalog, None, "syzygy.capability", {"name": "health.write"})
        self.assertEqual(owner["result"]["owner_record"]["owner"], "syzygy-health-workbook")
        safety = call(self.catalog, None, "syzygy.safety", {"name": "roarm"})
        self.assertIn("does not send motion", " ".join(safety["result"]["safety"]))
        state = call(self.catalog, None, "syzygy.state")["result"]
        self.assertFalse(state["probed"])
        self.assertTrue(all(item["availability"] == "declared" for item in state["subsystems"]))
        unknown = handle_rpc(self.catalog, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "shell.exec", "arguments": {}},
        })
        self.assertEqual(unknown["error"]["message"], "UNKNOWN_CAPABILITY")
        self.assertEqual(call(self.catalog, None, "health.delete")["reason"], "UNKNOWN_CAPABILITY")

    def test_health_read_write_audit_and_protected_sheets(self):
        fake = FakeHealth()
        client = bridge(fake)
        read = call(self.catalog, client, "health.read", {"sheet": "Weight Trend"})
        self.assertTrue(read["accepted"])
        self.assertEqual(read["result"]["sheet"], "Weight Trend")
        self.assertIn("/v1/sheets/weight-trend", fake.calls[0][1])
        self.assertEqual(fake.calls[0][2], "read-token")
        blocked = call(self.catalog, client, "health.write", {
            "sheet": "Deficit Bank",
            "values": {"note": "no"},
            "source": "registry",
            "updated_by": "tester",
            "recorded_at": "2026-10-04T00:00:00Z",
        }, profile="operate")
        self.assertEqual(blocked["reason"], "protected_sheet")
        self.assertEqual(len(fake.calls), 1)
        missing = call(self.catalog, client, "health.write", {
            "sheet": "notes",
            "values": {"note": "hello"},
            "source": "registry",
            "updated_by": "tester",
            "recorded_at": "2026-10-04T00:00:00Z",
        })
        self.assertEqual(missing["reason"], "PERMISSION_DENIED")
        written = call(self.catalog, client, "health.write", {
            "sheet": "notes",
            "values": {"note": "hello"},
            "source": "registry",
            "updated_by": "tester",
            "recorded_at": "2026-10-04T00:00:00Z",
        }, profile="operate")
        self.assertTrue(written["accepted"])
        method, url, token, body = fake.calls[-1]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/v1/notes"))
        self.assertEqual(token, "maintain-token")
        self.assertEqual(body["source"], "registry")
        self.assertNotIn("delete", body)
        audit = call(self.catalog, client, "health.audit", {"sheet": "Notes"})
        self.assertEqual(audit["result"]["entries"][0]["action"], "append")
        self.assertTrue(fake.calls[-1][1].startswith("http://127.0.0.1:5052/v1/audit"))

    def test_roarm_motion_is_restricted_and_hosts_are_declared(self):
        motion = call(self.catalog, None, "roarm.motion", {"skill": "move_to_pose"}, profile="operate")
        self.assertEqual(motion["reason"], "MOTION_RESTRICTED")
        self.assertIn("holding Z", " ".join(motion["safety"]))
        skills = call(self.catalog, None, "roarm.skills")["result"]["skills"]
        self.assertEqual(skills, ["move_to_pose", "return_home", "run_pattern", "stop", "clear"])
        cameras = call(self.catalog, None, "camera.list")["result"]["cameras"]
        encoded = json.dumps(cameras).casefold()
        self.assertIn("backyard", encoded)
        self.assertNotIn("rtsp", encoded)
        self.assertNotIn("password", encoded)
        snapshot = call(self.catalog, None, "camera.snapshot", {"camera_id": "indoor"}, profile="operate")
        self.assertEqual(snapshot["reason"], "CAMERA_RESTRICTED")
        pi = call(self.catalog, None, "pi.status")["result"]
        jetson = call(self.catalog, None, "jetson.status")["result"]
        self.assertIn("fitbit-mcp", pi["services"])
        self.assertIn("vision-hub", jetson["services"])
        self.assertFalse(pi["probed"])
        self.assertFalse(jetson["probed"])
        self.assertEqual(pi["host"], "192.168.1.18")
        self.assertEqual(jetson["host"], "192.168.1.17")

    def test_manifest_rejects_a_shell_proxy(self):
        document = json.loads(json.dumps(self.catalog.document))
        document["capabilities"].append({
            "name": "shell.exec",
            "subsystem": "pi",
            "permission": "write",
            "execution": "proxy",
            "owner": "pi",
            "endpoint": "http://127.0.0.1",
            "docs": "docs/CAPABILITIES.md",
            "availability": "declared",
            "safety": ["no"],
            "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        })
        with self.assertRaises(CatalogError):
            Catalog(document)

    def test_public_row_id_is_patched_and_conflicts_name_that_id(self):
        fake = FakeHealth()
        patched = call(self.catalog, bridge(fake), "health.write", {
            "sheet": "daily",
            "row_id": "daily:21",
            "values": {"hrv_ms": 22.3},
            "reason": "test patch",
            "source": "registry",
            "updated_by": "tester",
            "recorded_at": "2026-10-06T00:00:00Z",
        }, profile="operate")
        self.assertTrue(patched["accepted"])
        method, url, _token, body = fake.calls[-1]
        self.assertEqual(method, "PATCH")
        self.assertTrue(url.endswith("/v1/daily/daily:21"))
        self.assertNotIn("%3A", url)
        self.assertEqual(body["fields"], {"hrv_ms": 22.3})
        self.assertEqual(body["reason"], "test patch")

        def conflict_transport(method, url, token, body):
            return 409, {
                "error": "conflict",
                "detail": "Daily already has a different row for this identity. Patch row_id daily:21.",
                "row_id": "daily:21",
            }

        health = HealthBridge("http://127.0.0.1:5052", "read-token", "maintain-token", conflict_transport)
        conflict = call(self.catalog, health, "health.write", {
            "sheet": "daily",
            "values": {"date": "2026-10-06", "hrv_ms": 22.3},
            "source": "registry",
            "updated_by": "tester",
            "recorded_at": "2026-10-06T00:00:00Z",
        }, profile="operate")
        self.assertFalse(conflict["accepted"])
        self.assertEqual(conflict["reason"], "conflict")
        self.assertEqual(conflict["row_id"], "daily:21")


WRITE = {
    "sheet": "notes",
    "values": {"note": "hello"},
    "source": "registry",
    "updated_by": "tester",
    "recorded_at": "2026-10-04T00:00:00Z",
    "profile": "operate",
}

GRANTS = Grants(
    discover_token="discover-secret",
    operate_token="operate-secret",
    discover_identity="workbook-reader",
    operate_identity="workbook-maintainer",
    discover_actor="portal-discover",
    operate_actor="portal-operate",
)


def unwrap_tool(test, response):
    result = response["result"]
    test.assertIn("content", result)
    test.assertEqual(result["content"][0]["type"], "text")
    payload = json.loads(result["content"][0]["text"])
    test.assertEqual(result["content"][0]["text"], json.dumps(payload))
    test.assertEqual(result["isError"], payload.get("accepted") is False)
    return payload


def tool_call(catalog, name, arguments, headers, health=None, write_log=None, grants=GRANTS):
    return handle_rpc(
        catalog,
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": name, "profile": "operate", "arguments": arguments},
        },
        health,
        headers,
        grants,
        write_log,
    )


class AuthorizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog.load()

    def test_equal_credentials_are_rejected(self):
        with self.assertRaises(ValueError):
            Grants(discover_token="same", operate_token="same")

    def test_discover_cannot_escalate_with_a_requested_operate_profile(self):
        fake = FakeHealth()
        response = tool_call(
            self.catalog,
            "health.write",
            WRITE,
            {"Authorization": "Bearer discover-secret"},
            bridge(fake),
        )
        denied = unwrap_tool(self, response)
        self.assertEqual(denied["reason"], "PERMISSION_DENIED")
        self.assertTrue(response["result"]["isError"])
        self.assertEqual(fake.calls, [])
        anonymous = tool_call(self.catalog, "health.write", WRITE, {}, bridge(fake))
        self.assertEqual(unwrap_tool(self, anonymous)["reason"], "PERMISSION_DENIED")
        unknown = tool_call(
            self.catalog,
            "health.write",
            WRITE,
            {"Authorization": "Bearer not-a-credential"},
            bridge(fake),
        )
        self.assertEqual(unwrap_tool(self, unknown)["reason"], "PERMISSION_DENIED")
        self.assertEqual(fake.calls, [])

    def test_authenticated_operate_identity_can_write_and_is_logged(self):
        fake = FakeHealth()
        write_log = []
        response = tool_call(
            self.catalog,
            "health.write",
            WRITE,
            {"Authorization": "Bearer operate-secret"},
            bridge(fake),
            write_log,
        )
        written = unwrap_tool(self, response)
        self.assertTrue(written["accepted"])
        self.assertFalse(response["result"]["isError"])
        self.assertEqual(fake.calls[-1][2], "maintain-token")
        self.assertEqual(write_log, [{
            "event": "health_write",
            "actor": "portal-operate",
            "identity": "workbook-maintainer",
            "profile": "operate",
            "capability": "health.write",
            "accepted": True,
        }])
        published = json.dumps({"response": response, "log": write_log})
        self.assertNotIn("maintain-token", published)
        self.assertNotIn("operate-secret", published)
        self.assertNotIn("discover-secret", published)

    def test_discover_can_read_and_audit(self):
        fake = FakeHealth()
        client = bridge(fake)
        headers = {"Authorization": "Bearer discover-secret"}
        read = tool_call(self.catalog, "health.read", {"sheet": "Weight Trend"}, headers, client)
        audit = tool_call(self.catalog, "health.audit", {"sheet": "Notes"}, headers, client)
        read_payload = unwrap_tool(self, read)
        self.assertTrue(read_payload["accepted"])
        self.assertFalse(read["result"]["isError"])
        self.assertEqual(read_payload["result"]["sheet"], "Weight Trend")
        audit_payload = unwrap_tool(self, audit)
        self.assertTrue(audit_payload["accepted"])
        self.assertFalse(audit["result"]["isError"])
        self.assertEqual(audit_payload["result"]["entries"][0]["action"], "append")
        self.assertEqual(fake.calls[0][2], "read-token")
        self.assertEqual(fake.calls[1][2], "read-token")

    def test_restricted_tools_stay_refused_for_operate(self):
        headers = {"Authorization": "Bearer operate-secret"}
        motion = tool_call(self.catalog, "roarm.motion", {"skill": "move_to_pose"}, headers)
        snapshot = tool_call(self.catalog, "camera.snapshot", {"camera_id": "indoor"}, headers)
        control = tool_call(self.catalog, "camera.control", {"camera_id": "indoor"}, headers)
        self.assertEqual(unwrap_tool(self, motion)["reason"], "MOTION_RESTRICTED")
        self.assertTrue(motion["result"]["isError"])
        self.assertEqual(unwrap_tool(self, snapshot)["reason"], "CAMERA_RESTRICTED")
        self.assertEqual(unwrap_tool(self, control)["reason"], "CAMERA_RESTRICTED")

    def test_unknown_and_unpinned_tools_stay_unavailable(self):
        headers = {"Authorization": "Bearer operate-secret"}
        for name in ("shell.exec", "health.delete", "delete_detection_image", "run_lissajous"):
            response = tool_call(self.catalog, name, {}, headers)
            self.assertEqual(response["error"]["message"], "UNKNOWN_CAPABILITY", name)


if __name__ == "__main__":
    unittest.main()
