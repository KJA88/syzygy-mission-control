"""Offline Vision Hub adapter. No Jetson connection."""
import importlib.util
import io
import json
import threading
import unittest
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
        self.assertEqual(backyard["stream_url"], "/api/perception/stream/backyard")
        self.assertIsNone(backyard["hub_url"])
        self.assertIsNone(view["hub_url"])
        front = view["cameras"][1]
        self.assertEqual(front["capabilities"], ["stream", "detect", "snapshot"])
        self.assertEqual(front["health"], "offline")
        gate = view["cameras"][2]
        self.assertEqual(gate["capabilities"], ["stream", "detect", "snapshot"])
        self.assertEqual(gate["health"], "unknown")
        encoded = json.dumps(view)
        self.assertNotIn("http://vision", encoded)
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

    def test_browser_camera_payload_has_no_jetson_origin(self):
        view = normalize_cameras(
            {"cameras": {"backyard": {"name": "Back Yard", "type": "ptz", "rtsp_url": "rtsp://secret"}}},
            {"backyard": {"online": True, "mode": "ACTIVE"}},
            [],
            service_base="http://192.168.1.17:8081",
            hub_ui="http://192.168.1.17:8080/",
        )
        encoded = json.dumps(view)
        self.assertEqual(view["cameras"][0]["stream_url"], "/api/perception/stream/backyard")
        self.assertIsNone(view["hub_url"])
        self.assertNotIn("192.168.1.17", encoded)
        self.assertNotIn("8081", encoded)
        self.assertNotIn("rtsp://", encoded)

    def test_stream_proxy_relays_multipart_and_degrades(self):
        frame = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8\xff\xd9\r\n"
        config = {"cameras": {
            "backyard": {"name": "Back Yard", "type": "fixed"},
            "frontyard": {"name": "Front Yard", "type": "fixed"},
        }}

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                if self.path == "/api/config":
                    body = json.dumps(config).encode("utf-8")
                elif self.path == "/api/cameras/status":
                    body = b"{}"
                elif self.path.startswith("/api/events"):
                    body = b"[]"
                else:
                    body = None
                if body is not None:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == "/stream/backyard":
                    self.send_response(200)
                    self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                    self.send_header("Content-Length", str(len(frame)))
                    self.end_headers()
                    self.wfile.write(frame)
                    return
                if self.path == "/stream/frontyard":
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        spec = importlib.util.spec_from_file_location("mc_stream_server", ROOT / "ui" / "server.py")
        server = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(server)
        origin = "http://127.0.0.1:%d" % upstream.server_port
        vision = VisionHub(origin, origin, timeout=2)
        handler = server.make_handler(ROOT / "state", ROOT / "ui", 120, 20, vision=vision)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % httpd.server_port
        try:
            with urllib.request.urlopen(base + "/api/perception/stream/backyard", timeout=2) as response:
                body = response.read()
                content_type = response.headers["Content-Type"]
                cache = response.headers["Cache-Control"]
            self.assertEqual(content_type, "multipart/x-mixed-replace; boundary=frame")
            self.assertEqual(cache, "no-store")
            self.assertIn(b"\xff\xd8\xff\xd9", body)
            request = urllib.request.Request(base + "/api/perception/stream/missing")
            with self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(request, timeout=2)
            self.assertEqual(missing.exception.code, 404)
            self.assertEqual(json.loads(missing.exception.read().decode("utf-8"))["reason"], "UNKNOWN_CAMERA")
            missing.exception.close()
            with self.assertRaises(urllib.error.HTTPError) as down:
                urllib.request.urlopen(base + "/api/perception/stream/frontyard", timeout=2)
            self.assertEqual(down.exception.code, 503)
            self.assertEqual(json.loads(down.exception.read().decode("utf-8"))["reason"], "VISION_HUB_UNAVAILABLE")
            down.exception.close()
            with urllib.request.urlopen(base + "/api/perception/cameras", timeout=2) as response:
                cameras = json.loads(response.read().decode("utf-8"))
            encoded = json.dumps(cameras)
            self.assertEqual(cameras["cameras"][0]["stream_url"], "/api/perception/stream/backyard")
            self.assertNotIn("127.0.0.1:%d" % upstream.server_port, encoded)

            class Drop:
                def write(self, _chunk):
                    raise BrokenPipeError("closed")

                def flush(self):
                    return None

            class Pieces:
                def __init__(self):
                    self.closed = False
                    self.sizes = []
                    self._pending = [b"\xff\xd8"]

                def read(self, size=None):
                    if size is None:
                        raise AssertionError("stream was buffered")
                    self.sizes.append(size)
                    if not self._pending:
                        return b""
                    return self._pending.pop(0)

                def close(self):
                    self.closed = True

            pieces = Pieces()
            with self.assertRaises(BrokenPipeError):
                server.relay_stream(Drop(), pieces)
            self.assertTrue(pieces.closed)
            self.assertEqual(pieces.sizes, [8192])
        finally:
            httpd.shutdown()
            httpd.server_close()
            upstream.shutdown()
            upstream.server_close()

    def test_snapshot_archive_is_separate_from_events(self):
        from perception.hub import public_events, public_gallery
        events = public_events([
            {"camera": "backyard", "class": "person", "confidence": 0.5, "timestamp": "t", "image": "detections/backyard/a.jpg"},
            {"camera": "door", "class": "opened", "confidence": 1, "timestamp": "t2"},
            {"camera": "x", "class": "y", "image": "/home/KA_PI/secret.jpg"},
        ])
        self.assertTrue(events[0]["snapshot"])
        self.assertFalse(events[1]["snapshot"])
        self.assertIsNone(events[2]["image"])
        self.assertFalse(events[2]["snapshot"])
        gallery = public_gallery([
            {"path": "detections/backyard/a.jpg", "camera": "backyard", "name": "a.jpg", "ts": "t"},
            {"path": "archive/backyard/b.jpg", "camera": "backyard", "name": "b.jpg", "ts": "t"},
            {"path": "rtsp://camera", "camera": "backyard", "name": "nope"},
            {"path": "/var/lib/a.jpg", "camera": "backyard"},
        ])
        self.assertEqual([item["archived"] for item in gallery], [False, True])
        encoded = json.dumps(gallery + events)
        self.assertNotIn("rtsp://", encoded)
        self.assertNotIn("/home/", encoded)
        self.assertNotIn("/var/", encoded)

        client, calls = self.hub({
            "http://hub/api/events/retain": response(200, {"ok": True}),
            "http://hub/api/events?limit=50": response(200, [
                {"camera": "door", "class": "opened", "timestamp": "t2"},
            ]),
            "http://hub/api/gallery/archive": response(200, {"ok": True, "paths": ["archive/backyard/a.jpg"]}),
            "http://hub/api/gallery/delete": response(200, {"ok": True}),
            "http://hub/api/gallery/clear-unarchived": response(200, {"ok": True}),
            "http://hub/api/events/clear": response(200, {"ok": True}),
            "http://hub/snapshots/detections/backyard/a.jpg": response(200, b"\xff\xd8jpeg", "image/jpeg"),
            "http://hub/snapshots/archive/backyard/b.jpg": response(200, b"\xff\xd8arch", "image/jpeg"),
        })
        listed = client.events()
        self.assertFalse(listed["events"][0]["snapshot"])
        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(calls[0][1], "http://hub/api/events/retain")
        self.assertIn(b'"max_records": 200', calls[0][2])
        self.assertEqual(client.archive_snapshots(["detections/backyard/a.jpg"])["paths"], ["archive/backyard/a.jpg"])
        self.assertEqual(client.archive_snapshots(["archive/backyard/b.jpg"])["reason"], "ARCHIVED_PROTECTED")
        self.assertEqual(client.delete_snapshots(["archive/backyard/b.jpg"])["reason"], "ARCHIVED_PROTECTED")
        self.assertEqual(client.delete_snapshots(["../secret.jpg"])["reason"], "MALFORMED_PARAMETERS")
        self.assertTrue(client.delete_snapshots(["detections/backyard/a.jpg"])["accepted"])
        self.assertEqual(client.clear_unarchived_snapshots()["reason"], "UNARCHIVED_CLEARED")
        self.assertEqual(client.clear_events()["reason"], "EVENTS_CLEARED")
        posted = [call[1] for call in calls if call[0] == "POST"]
        self.assertEqual(posted.count("http://hub/api/gallery/delete"), 1)
        self.assertIn("http://hub/api/gallery/clear-unarchived", posted)
        self.assertNotIn("http://hub/api/gallery/clear", posted)
        self.assertEqual(posted.count("http://hub/api/events/clear"), 1)
        body, reason = client.download_snapshots(["detections/backyard/a.jpg", "archive/backyard/b.jpg"])
        self.assertIsNone(reason)
        with zipfile.ZipFile(io.BytesIO(body)) as bundle:
            self.assertEqual(bundle.namelist(), ["detections/backyard/a.jpg", "archive/backyard/b.jpg"])
            self.assertTrue(bundle.read("archive/backyard/b.jpg").startswith(b"\xff\xd8"))
        self.assertNotIn(b"192.168", body)
        self.assertNotIn(b"rtsp://", body)


if __name__ == "__main__":
    unittest.main()
