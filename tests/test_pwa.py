"""Installable Mission Control shell. Live API responses stay uncached."""
import importlib.util
import json
import struct
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _server():
    spec = importlib.util.spec_from_file_location("mc_pwa_server", ROOT / "ui" / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _png_size(path):
    data = path.read_bytes()
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    return struct.unpack(">II", data[16:24])


class PwaTests(unittest.TestCase):
    def test_manifest_and_icons_are_installable(self):
        manifest = json.loads((ROOT / "ui" / "manifest.webmanifest").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "SYZYGY Mission Control")
        self.assertEqual(manifest["short_name"], "SYZYGY")
        self.assertEqual(manifest["start_url"], "/")
        self.assertEqual(manifest["scope"], "/")
        self.assertEqual(manifest["display"], "standalone")
        self.assertEqual(manifest["orientation"], "any")
        self.assertEqual(manifest["theme_color"], "#070b10")
        icons = {(icon["sizes"], icon["purpose"]): icon["src"] for icon in manifest["icons"]}
        self.assertEqual(icons[("192x192", "any")], "/icons/icon-192.png")
        self.assertEqual(icons[("512x512", "any")], "/icons/icon-512.png")
        self.assertEqual(icons[("512x512", "maskable")], "/icons/icon-maskable-512.png")
        self.assertEqual(_png_size(ROOT / "ui" / "icons" / "icon-192.png"), (192, 192))
        self.assertEqual(_png_size(ROOT / "ui" / "icons" / "icon-512.png"), (512, 512))
        self.assertEqual(_png_size(ROOT / "ui" / "icons" / "icon-maskable-512.png"), (512, 512))

    def test_service_worker_never_caches_api_or_writes(self):
        text = (ROOT / "ui" / "sw.js").read_text(encoding="utf-8")
        page = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "ui" / "app.js").read_text(encoding="utf-8")
        api_guard = text.index('url.pathname.indexOf("/api/") === 0')
        respond = text.index("event.respondWith")
        self.assertLess(text.index('request.method !== "GET"'), respond)
        self.assertLess(api_guard, respond)
        self.assertIn("syzygy-shell-v1", text)
        self.assertNotIn("localStorage", text)
        self.assertNotIn("HA_TOKEN", text)
        self.assertIn(
            '<link rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials" />',
            page,
        )
        self.assertIn('navigator.serviceWorker.register("/sw.js")', script)
        self.assertIn('typeof navigator !== "undefined"', script)
        self.assertIn('fetch("/api/workouts/recent?limit=8", { cache: "no-store" })', script)
        self.assertIn('fetch("/api/workouts/summary?days=7", { cache: "no-store" })', script)
        self.assertIn("refreshWorkouts();", script)
        self.assertNotIn("await refreshWorkouts()", script)

    def test_static_pwa_assets_use_explicit_types(self):
        server = _server()
        directory = Path(tempfile.mkdtemp())
        handler = server.make_handler(directory, ROOT / "ui", 120, 20)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % httpd.server_port
        try:
            manifest = _get(base + "/manifest.webmanifest")
            self.assertTrue(manifest["type"].startswith("application/manifest+json"))
            self.assertEqual(json.loads(manifest["body"])["short_name"], "SYZYGY")
            worker = _get(base + "/sw.js")
            self.assertTrue(worker["type"].startswith("text/javascript"))
            self.assertIn(b"/api/", worker["body"])
            icon = _get(base + "/icons/icon-192.png")
            self.assertEqual(icon["type"], "image/png")
            self.assertTrue(icon["body"].startswith(b"\x89PNG\r\n\x1a\n"))
            health = _get(base + "/api/health")
            self.assertTrue(health["type"].startswith("application/json"))
            self.assertTrue(json.loads(health["body"])["ok"])
        finally:
            httpd.shutdown()
            httpd.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=2) as response:
        return {"type": response.headers.get("Content-Type", ""), "body": response.read()}


if __name__ == "__main__":
    unittest.main()
