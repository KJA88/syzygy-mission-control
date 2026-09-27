"""Offline Vision Hub adapter. No Jetson connection."""
import importlib.util
import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from perception.hub import VisionHub, devices_from, normalize_cameras

ROOT = Path(__file__).resolve().parents[1]


def response(status, payload, content_type="application/json"):
    class Response:
        def __init__(self):
            self.status = status
            self.headers = {"Content-Type": content_type}
            self._body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    return Response()


def opener(routes, calls):
    def open_request(request, timeout=3):
        calls.append((request.get_method(), request.full_url, request.data))
        for prefix, payload in routes.items():
            if request.full_url.startswith(prefix):
                return payload
        raise OSError("missing " + request.full_url)

    return open_request


CONFIG = {
    "cameras": {
        "backyard": {
            "name": "Back Yard PTZ",
            "type": "ptz",
            "tracking": False,
            "snapshots": True,
            "rtsp_url": "rtsp://secret",
            "ptz_user": "admin",
            "ptz_pass": "hidden",
        },
        "frontyard": {
            "name": "Front Yard",
            "type": "fixed",
            "tracking": False,
            "snapshots": True,
            "rtsp_url": "rtsp://front",
        },
        "gate": {
            "name": "Gate",
            "type": "fixed",
            "monitor_only": True,
            "snapshots": False,
        },
    }
}


class PerceptionTests(unittest.TestCase):
    def hub(self, routes):
        calls = []
        client = VisionHub(
            "http://hub",
            "http://vision",
            "http://hub/",
            {"backyard": {"parent_id": "roarm-1"}},
            opener(routes, calls),
        )
        return client, calls

    def test_normalizes_dynamic_cameras_and_strips_secrets(self):
        client, _calls = self.hub({
            "http://hub/api/config": response(200, CONFIG),
            "http://hub/api/cameras/status": response(200, {
                "backyard": {"online": True, "mode": "ACTIVE"},
                "frontyard": {"online": False, "mode": "offline"},
            }),
            "http://hub/api/events?limit=50": response(200, [
                {"camera": "backyard", "class": "person", "confidence": 0.8, "timestamp": "t", "image": "detections/backyard/a.jpg"},
            ]),
        })
        view = client.camera_view()
        self.assertTrue(view["available"])
        ids = [camera["id"] for camera in view["cameras"]]
        self.assertEqual(ids, ["backyard", "frontyard", "gate"])
        backyard = view["cameras"][0]
        self.assertEqual(backyard["capabilities"], ["stream", "detect", "snapshot", "ptz", "track"])
        self.assertEqual(backyard["parent_id"], "roarm-1")
        self.assertEqual(backyard["stream_url"], "http://vision/stream/backyard")
        front = view["cameras"][1]
        self.assertEqual(front["capabilities"], ["stream", "detect", "snapshot"])
        self.assertEqual(front["health"], "offline")
        gate = view["cameras"][2]
        self.assertEqual(gate["capabilities"], ["stream", "detect", "snapshot"])
        self.assertEqual(gate["health"], "unknown")
        encoded = json.dumps(view)
        self.assertNotIn("rtsp://", encoded)
        self.assertNotIn("ptz_pass", encoded)
        self.assertNotIn("hidden", encoded)
        self.assertNotIn("admin", encoded)

    def test_ptz_and_track_are_rejected_without_the_capability(self):
        routes = {
            "http://hub/api/config": response(200, CONFIG),
            "http://hub/api/cameras/status": response(200, {}),
            "http://hub/api/events?limit=50": response(200, []),
            "http://hub/ptz/backyard": response(200, {"ok": True}),
            "http://hub/api/config/backyard": response(200, {"ok": True}),
        }
        client, calls = self.hub(routes)
        self.assertEqual(client.ptz("frontyard", "left")["reason"], "CAPABILITY_UNAVAILABLE")
        self.assertEqual(client.track("frontyard", True)["reason"], "CAPABILITY_UNAVAILABLE")
        self.assertEqual(client.ptz("backyard", "spin")["reason"], "MALFORMED_PARAMETERS")
        self.assertTrue(client.ptz("backyard", "left")["accepted"])
        self.assertTrue(client.track("backyard", False)["accepted"])
        posted = [call for call in calls if call[0] == "POST"]
        self.assertEqual(posted[0][1], "http://hub/ptz/backyard")
        self.assertIn(b'"tracking": false', posted[1][2])
        self.assertTrue(all("/api/cameras/status" not in call[1] and "/api/events" not in call[1] for call in calls))

    def test_ptz_stop_does_not_prefetch_status_or_events(self):
        client, calls = self.hub({
            "http://hub/api/config": response(200, CONFIG),
            "http://hub/api/cameras/status": response(200, {}),
            "http://hub/api/events?limit=50": response(200, []),
            "http://hub/ptz/backyard": response(200, {"ok": True}),
        })
        self.assertTrue(client.ptz("backyard", "stop")["accepted"])
        self.assertEqual(
            [(call[0], call[1]) for call in calls],
            [("GET", "http://hub/api/config"), ("POST", "http://hub/ptz/backyard")],
        )
        self.assertIn(b'"dir": "stop"', calls[1][2])
        calls.clear()
        self.assertTrue(client.ptz("backyard", "stop")["accepted"])
        self.assertEqual([(call[0], call[1]) for call in calls], [("POST", "http://hub/ptz/backyard")])

    def test_offline_and_malformed_hub_are_safe(self):
        calls = []

        def broken(_request, timeout=3):
            raise OSError("down")

        client = VisionHub("http://hub", "http://vision", opener=broken)
        view = client.camera_view()
        self.assertFalse(view["available"])
        self.assertEqual(view["cameras"], [])
        self.assertEqual(view["reason"], "VISION_HUB_UNAVAILABLE")

        def malformed(request, timeout=3):
            if request.full_url.endswith("/api/config"):
                return response(200, b"not-json", "text/plain")
            return response(200, {})

        client = VisionHub("http://hub", "http://vision", opener=malformed)
        self.assertEqual(client.camera_view()["reason"], "VISION_HUB_MALFORMED")
        self.assertEqual(client.events()["events"], [])

    def test_unknown_camera_and_snapshot_validation(self):
        client, _calls = self.hub({
            "http://hub/api/config": response(200, CONFIG),
            "http://hub/api/cameras/status": response(200, {}),
            "http://hub/api/events?limit=50": response(200, []),
            "http://vision/snapshot/frontyard": response(200, b"\xff\xd8jpeg", "image/jpeg"),
            "http://vision/snapshot/gate": response(200, b"\xff\xd8gate", "image/jpeg"),
        })
        self.assertEqual(client.ptz("../etc", "left")["reason"], "UNKNOWN_CAMERA")
        body, reason = client.snapshot("frontyard")
        self.assertTrue(body.startswith(b"\xff\xd8"))
        self.assertIsNone(reason)
        gate_body, gate_reason = client.snapshot("gate")
        self.assertTrue(gate_body.startswith(b"\xff\xd8"))
        self.assertIsNone(gate_reason)
        media, media_reason = client.media("../cameras_config.json")
        self.assertIsNone(media)
        self.assertEqual(media_reason, "MALFORMED_PARAMETERS")

    def test_devices_include_new_cameras_and_the_arm(self):
        view = normalize_cameras(
            {"cameras": {"dock": {"name": "Dock", "type": "fixed", "snapshots": True}}},
            {"dock": {"online": True, "mode": "ACTIVE"}},
            [],
            service_base="http://vision",
            hub_ui="http://hub/",
        )
        devices = devices_from(view["cameras"], {"status": "green", "reachability": "reachable", "connected": True})
        self.assertEqual(devices[0]["provider_id"], "dock")
        self.assertNotIn("ptz", devices[0]["capabilities"])
        self.assertEqual(devices[1]["id"], "arm:roarm-1")
        self.assertEqual(devices[1]["health"], "online")
        encoded = json.dumps(devices)
        self.assertNotIn("password", encoded)

    def test_mission_control_routes_and_static_shell(self):
        page = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        for label in ("Overview", "Perception", "Robots", "Devices", "Events", "System", "Settings"):
            self.assertIn(label, page)
        self.assertIn("RoArm control", page)
        self.assertIn("/api/perception/cameras", script)
        self.assertNotIn("192.168.1.17", script)
        self.assertNotIn("rtsp://", page)
        signature = script[script.index("function cameraSignature"):script.index("function cameraCard")]
        for field in ("camera.id", "camera.name", "camera.parent_id", "camera.capabilities", "camera.stream_url"):
            self.assertIn(field, signature)
        release = script[script.index("function releasePtz"):script.index("function holdPtz")]
        self.assertIn('sendPtz(camera, "stop")', release)
        self.assertNotIn("await", release)
        for event_name in ("pointerdown", "mousedown", "touchstart", "pointerup", "pointercancel", "touchend", "mouseleave", "blur"):
            self.assertIn(event_name, script)
        self.assertIn("setInterval", script)
        self.assertIn("clearInterval", script)
        spec = importlib.util.spec_from_file_location("mc_perception_server", ROOT / "ui" / "server.py")
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)

        class FakeVision:
            def camera_view(self):
                return {"available": True, "cameras": [{"id": "dock", "name": "Dock", "capabilities": ["stream"]}], "hub_url": "http://hub/"}

            def ptz(self, camera, direction):
                return {"accepted": False, "reason": "CAPABILITY_UNAVAILABLE", "camera": camera, "dir": direction}

            def track(self, camera, enabled):
                return {"accepted": True, "camera": camera, "tracking": enabled}

        handler = server.make_handler(ROOT / "state", ROOT / "ui", 120, 20, owner=None, vision=FakeVision())
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps({"camera": "frontyard", "dir": "left"}).encode("utf-8")
            request = urllib.request.Request(
                "http://127.0.0.1:%d/api/perception/ptz" % httpd.server_port,
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=2) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(payload["reason"], "CAPABILITY_UNAVAILABLE")
            with urllib.request.urlopen("http://127.0.0.1:%d/api/perception/cameras" % httpd.server_port, timeout=2) as response:
                cameras = json.loads(response.read().decode("utf-8"))
            self.assertEqual(cameras["cameras"][0]["id"], "dock")
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
