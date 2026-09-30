#!/usr/bin/env python3
"""SYZYGY Mission Control UI and RoArm skill API.

Serves static UI assets and Guardian outputs (snapshot.json + events.jsonl).
Named skill requests and the engineering JSON path are served on this same
server. Does not probe Guardian MCP catalogs. Read-only workout summaries may
call the local Fitbit MCP at 127.0.0.1:8010 and do not receive OAuth tokens.

The origin stays on the Pi. A phone may reach only this UI through the
dedicated Cloudflare Access hostname and tunnel. Do not publish port 9070
directly, and do not publish Home Assistant, RoArm, Vision Hub, MQTT, or MCP
ports. Local LAN access stays available.

Mirrors guardian.storage.read_snapshot heartbeat freshness:
if guardian.heartbeat_at is missing or older than hard_stale seconds (default 120),
system.status and guardian.status become unknown (GUARDIAN_HEARTBEAT_STALE / SNAPSHOT_STALE).
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9070
DEFAULT_HARD_STALE_S = 120
DEFAULT_EVENTS_LIMIT = 50
UI_DIR = Path(__file__).resolve().parent
STATIC_TYPES = {
    ".webmanifest": "application/manifest+json",
    ".js": "text/javascript",
    ".png": "image/png",
}


def _static_type(path: Path) -> str:
    explicit = STATIC_TYPES.get(path.suffix.lower())
    ctype = explicit or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript", "application/json", "application/manifest+json"):
        return ctype + "; charset=utf-8"
    return ctype


def utcnow() -> float:
    return datetime.now(timezone.utc).timestamp()


def age_seconds(ts, now: float):
    """Return age in seconds, or None if unparseable / clock skew > 5s."""
    if not ts:
        return None
    try:
        value = now - datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
        return value if value >= -5 else None
    except (ValueError, TypeError, AttributeError, OSError):
        return None


def mark_state_engine_stale(snapshot: dict) -> None:
    state = snapshot.get("state_engine")
    if not isinstance(state, dict):
        return
    for entity in state.get("entities", []):
        if not isinstance(entity, dict):
            continue
        attributes = entity.get("attributes")
        if not isinstance(attributes, dict):
            continue
        for assertion in attributes.values():
            if (
                isinstance(assertion, dict)
                and assertion.get("knowledge")
                in ("observed", "derived", "verified", "requested", "remembered")
            ):
                assertion["freshness"] = "stale"
    system = next(
        (
            entity
            for entity in state.get("entities", [])
            if isinstance(entity, dict) and entity.get("id") == "system/syzygy"
        ),
        None,
    )
    if isinstance(system, dict):
        health = (system.get("attributes") or {}).get("health")
        if isinstance(health, dict):
            health.update(
                value=None,
                knowledge="unknown",
                freshness="stale",
                confidence=None,
                reason="SNAPSHOT_STALE",
            )
    operational = state.get("operational")
    if isinstance(operational, dict):
        operational["freshness"] = "stale"
        if operational.get("state") in ("PREPARING", "MOVING"):
            operational["recorded_state"] = operational.get("state")
            operational["state"] = None
            operational["motion_permitted"] = False
            operational["display_reason"] = "UNPROVEN_ACTIVE_STATE"


def apply_heartbeat_freshness(snapshot: dict, now: float, hard_stale: float = DEFAULT_HARD_STALE_S) -> dict:
    """Mirror guardian.storage.read_snapshot freshness transform."""
    if not isinstance(snapshot, dict):
        return {"schema_version": 1, "system": {"status": "unknown", "reason": "INVALID_SNAPSHOT"},
                "guardian": {"status": "unknown", "class": "INVALID_SNAPSHOT"}}
    out = deepcopy(snapshot)
    guardian = out.setdefault("guardian", {})
    elapsed = age_seconds(guardian.get("heartbeat_at"), now)
    if elapsed is None or elapsed > hard_stale:
        out.setdefault("system", {}).update(status="unknown", reason="GUARDIAN_HEARTBEAT_STALE")
        guardian.update(status="unknown")
        guardian["class"] = "SNAPSHOT_STALE"
        guardian["observation_age_s"] = elapsed
        mark_state_engine_stale(out)
    elif elapsed is not None:
        guardian["observation_age_s"] = elapsed
    return out


def read_snapshot_file(path: Path, now: float | None = None, hard_stale: float = DEFAULT_HARD_STALE_S) -> dict:
    now = utcnow() if now is None else now
    if not path.is_file():
        return {
            "schema_version": 1,
            "generated_at": None,
            "trace_id": None,
            "guardian": {"status": "unknown", "class": "SNAPSHOT_MISSING", "heartbeat_at": None},
            "system": {"status": "unknown", "reason": "SNAPSHOT_MISSING"},
            "nodes": [],
            "services": [],
            "paths": [],
            "auth": [],
            "activity": [],
            "_ui": {"error": f"snapshot not found: {path}"},
        }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {
            "schema_version": 1,
            "guardian": {"status": "unknown", "class": "SNAPSHOT_UNREADABLE"},
            "system": {"status": "unknown", "reason": "SNAPSHOT_UNREADABLE"},
            "nodes": [],
            "services": [],
            "paths": [],
            "auth": [],
            "activity": [],
            "_ui": {"error": str(exc)},
        }
    return apply_heartbeat_freshness(raw, now, hard_stale)


def tail_jsonl(path: Path, limit: int = DEFAULT_EVENTS_LIMIT) -> list:
    """Return the last `limit` JSON objects from an append-only JSONL file."""
    if limit < 0:
        limit = 0
    if not path.is_file() or limit == 0:
        return []
    try:
        # Efficient-ish: read whole file for typical small event logs; trim to last N lines.
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    events = []
    for line in lines[-limit:]:
        try:
            events.append(json.loads(line))
        except ValueError:
            events.append({"raw": line, "class": "EVENT_MALFORMED", "severity": "warning"})
    return events


def _read_json_body(handler, limit=16384):
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError:
        return None, "MALFORMED_JSON"
    if length < 0 or length > limit:
        return None, "MALFORMED_JSON"
    try:
        raw = handler.rfile.read(length)
        payload = json.loads(raw.decode("utf-8") or "{}")
    except (UnicodeError, ValueError, OSError):
        return None, "MALFORMED_JSON"
    if not isinstance(payload, dict):
        return None, "MALFORMED_JSON"
    return payload, None


def _vision_from_config(root: Path):
    from perception.hub import VisionHub
    from guardian.config import load_config
    vision = {}
    try:
        loaded = load_config(root / "config" / "services.yaml")
        if isinstance(loaded.get("vision"), dict):
            vision = loaded["vision"]
    except (OSError, ValueError, KeyError, TypeError):
        vision = {}
    return VisionHub(
        vision.get("hub_base") or "",
        vision.get("service_base") or "",
        vision.get("hub_ui"),
        vision.get("cameras") if isinstance(vision.get("cameras"), dict) else {},
    )


def _home_down():
    return {
        "available": False,
        "reason": "HA_UNCONFIGURED",
        "ui_url": None,
        "counts": {},
        "entities": [],
    }


def _home_from_config(root: Path):
    from guardian.config import load_config
    from home.adapter import HomeAssistant, policy_from_config
    raw = {}
    try:
        loaded = load_config(root / "config" / "services.yaml")
        if isinstance(loaded.get("home_assistant"), dict):
            raw = loaded["home_assistant"]
    except (OSError, ValueError, KeyError, TypeError):
        raw = {}
    policy = policy_from_config(raw)
    return HomeAssistant(policy, timeout=policy["timeout"])


def _mission_health(snapshot_path: Path, hard_stale: float):
    from missions.health import required_health

    def health():
        snap = read_snapshot_file(snapshot_path, utcnow(), hard_stale)
        return required_health(snap)

    return health


def build_missions(state_dir: Path, home, hard_stale: float):
    from missions.engine import open_engine

    root = Path(__file__).resolve().parent.parent
    try:
        return open_engine(
            root / "config" / "missions.yaml",
            home,
            _mission_health(state_dir / "snapshot.json", hard_stale),
            state_dir / "mission-runs.json",
        )
    except Exception:
        return None


def _mission_post(missions, path, payload):
    if missions is None:
        return {"accepted": False, "reason": "MISSION_ENGINE_UNAVAILABLE"}
    if not isinstance(payload, dict):
        return {"accepted": False, "reason": "MALFORMED_PARAMETERS"}
    if path == "/api/missions/stop":
        if set(payload) - {"operator"}:
            return {"accepted": False, "reason": "MALFORMED_PARAMETERS"}
        return missions.stop()
    if set(payload) - {"mission", "operator"}:
        return {"accepted": False, "reason": "MALFORMED_PARAMETERS"}
    return missions.start(payload.get("mission"), operator=payload.get("operator"))


def _workouts_from_env():
    try:
        from workouts.fitbit import open_fitbit
        return open_fitbit()
    except Exception:
        return None


def _workout_offline():
    return {"available": False, "reason": "FITBIT_UNAVAILABLE", "source": "fitbit", "workouts": []}


def _workout_recent(workouts, qs):
    if set(qs) - {"limit"}:
        return 400, {"available": False, "reason": "MALFORMED_PARAMETERS", "source": "fitbit", "workouts": []}
    raw = (qs.get("limit") or ["8"])[0]
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        return 400, {"available": False, "reason": "MALFORMED_PARAMETERS", "source": "fitbit", "workouts": []}
    limit = max(1, min(limit, 20))
    if workouts is None:
        return 200, _workout_offline()
    try:
        return 200, workouts.recent(limit)
    except Exception:
        return 200, _workout_offline()


def _workout_summary(workouts, qs):
    if set(qs) - {"days"}:
        return 400, {"available": False, "reason": "MALFORMED_PARAMETERS", "source": "fitbit", "workouts": []}
    if "days" in qs and qs.get("days") != ["7"]:
        return 400, {"available": False, "reason": "MALFORMED_PARAMETERS", "source": "fitbit", "workouts": []}
    if workouts is None:
        return 200, _workout_offline()
    try:
        return 200, workouts.summary()
    except Exception:
        return 200, _workout_offline()


def relay_stream(destination, upstream, chunk_size=8192):
    """Copy an upstream MJPEG body in chunks and close it when the client stops."""
    try:
        while True:
            chunk = upstream.read(chunk_size)
            if not chunk:
                return
            destination.write(chunk)
            flush = getattr(destination, "flush", None)
            if callable(flush):
                flush()
    finally:
        closer = getattr(upstream, "close", None)
        if callable(closer):
            closer()


def make_handler(state_dir: Path, ui_dir: Path, hard_stale: float, events_limit: int, owner=None, vision=None, home=None, missions=None, workouts=None):
    snapshot_path = state_dir / "snapshot.json"
    events_path = state_dir / "events.jsonl"

    class Handler(BaseHTTPRequestHandler):
        server_version = "SyzygyMissionControl/0.1"

        def log_message(self, fmt, *args):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def _send(self, code: int, body: bytes, content_type: str, cache: str = "no-store"):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, payload):
            body = (json.dumps(payload, allow_nan=False) + "\n").encode("utf-8")
            self._send(code, body, "application/json; charset=utf-8")

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path.startswith("/api/"):
                return self._api(path, parsed.query)
            # Static assets under ui/
            rel = path.lstrip("/")
            if ".." in rel or rel.startswith("/"):
                return self._send(404, b"not found\n", "text/plain; charset=utf-8")
            return self._static(rel)

        def do_POST(self):
            parsed = urlparse(self.path)
            payload, error = _read_json_body(self)
            if error:
                return self._json(400, {"accepted": False, "result": "rejected", "reason": error})
            if parsed.path == "/api/perception/track":
                if vision is None:
                    return self._json(200, {"accepted": False, "reason": "VISION_HUB_UNAVAILABLE"})
                return self._json(200, vision.track(payload.get("camera"), payload.get("enabled")))
            if parsed.path == "/api/perception/ptz":
                if vision is None:
                    return self._json(200, {"accepted": False, "reason": "VISION_HUB_UNAVAILABLE"})
                return self._json(200, vision.ptz(payload.get("camera"), payload.get("dir")))
            if parsed.path == "/api/perception/snapshots/archive":
                if vision is None:
                    return self._json(200, {"accepted": False, "reason": "VISION_HUB_UNAVAILABLE"})
                return self._json(200, vision.archive_snapshots(payload.get("paths")))
            if parsed.path == "/api/perception/snapshots/delete":
                if vision is None:
                    return self._json(200, {"accepted": False, "reason": "VISION_HUB_UNAVAILABLE"})
                return self._json(200, vision.delete_snapshots(payload.get("paths")))
            if parsed.path == "/api/perception/snapshots/clear-unarchived":
                if vision is None:
                    return self._json(200, {"accepted": False, "reason": "VISION_HUB_UNAVAILABLE"})
                return self._json(200, vision.clear_unarchived_snapshots())
            if parsed.path == "/api/perception/snapshots/download":
                if vision is None:
                    return self._json(503, {"accepted": False, "reason": "VISION_HUB_UNAVAILABLE"})
                body, reason = vision.download_snapshots(payload.get("paths"))
                if body is None:
                    return self._json(400, {"accepted": False, "reason": reason})
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Disposition", "attachment; filename=\"snapshots.zip\"")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/perception/events/clear":
                if vision is None:
                    return self._json(200, {"accepted": False, "reason": "VISION_HUB_UNAVAILABLE"})
                return self._json(200, vision.clear_events())
            if parsed.path == "/api/home/action":
                if home is None:
                    return self._json(200, {"accepted": False, "reason": "HA_UNCONFIGURED"})
                return self._json(200, home.act(
                    payload.get("entity_id"),
                    payload.get("action"),
                    payload.get("brightness"),
                ))
            if parsed.path in ("/api/missions/start", "/api/missions/stop"):
                return self._json(200, _mission_post(missions, parsed.path, payload))
            if parsed.path.startswith("/api/workouts/"):
                return self._json(405, {"available": False, "reason": "READ_ONLY", "source": "fitbit"})
            if owner is None:
                return self._json(503, {"accepted": False, "result": "rejected", "reason": "CONTROL_OWNER_UNAVAILABLE"})
            if parsed.path == "/api/roarm/skills":
                result = owner.request(
                    payload.get("skill"),
                    authority=payload.get("authority"),
                    operator=payload.get("operator"),
                    mission=payload.get("mission"),
                    trace_id=payload.get("trace_id"),
                    params=payload.get("params") if isinstance(payload.get("params"), dict) else {},
                )
                return self._json(200, result)
            if parsed.path == "/api/roarm/engineering":
                result = owner.engineering(
                    payload.get("packet"),
                    authority=payload.get("authority"),
                    operator=payload.get("operator"),
                    mission=payload.get("mission"),
                    trace_id=payload.get("trace_id"),
                )
                return self._json(200, result)
            return self._json(404, {"error": "not found", "path": parsed.path})

        def do_HEAD(self):
            # Support HEAD for simple health checks without body.
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html", "/api/snapshot", "/api/events", "/api/health"):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

        def _api(self, path: str, query: str):
            qs = parse_qs(query)
            if path == "/api/perception/cameras":
                if vision is None:
                    return self._json(200, {"available": False, "reason": "VISION_HUB_UNAVAILABLE", "cameras": []})
                return self._json(200, vision.camera_view())
            if path == "/api/perception/events":
                if vision is None:
                    return self._json(200, {"available": False, "reason": "VISION_HUB_UNAVAILABLE", "events": []})
                try:
                    limit = int(qs.get("limit", ["50"])[0])
                except (TypeError, ValueError):
                    limit = 50
                return self._json(200, vision.events(max(1, min(limit, 100))))
            if path == "/api/perception/snapshots":
                if vision is None:
                    return self._json(200, {"available": False, "reason": "VISION_HUB_UNAVAILABLE", "snapshots": []})
                camera = (qs.get("camera") or [None])[0]
                return self._json(200, vision.snapshots(camera))
            if path == "/api/home/entities":
                if home is None:
                    return self._json(200, _home_down())
                return self._json(200, home.entity_view())
            if path == "/api/home/status":
                if home is None:
                    return self._json(200, {"available": False, "reason": "HA_UNCONFIGURED", "ui_url": None, "counts": {}})
                return self._json(200, home.status())
            if path == "/api/missions":
                return self._json(200, {
                    "missions": [] if missions is None else missions.list_definitions(),
                })
            if path == "/api/missions/status":
                if missions is None:
                    return self._json(200, {
                        "available": False,
                        "reason": "MISSION_ENGINE_UNAVAILABLE",
                        "state": "IDLE",
                        "stop_requested": False,
                        "active": None,
                    })
                return self._json(200, missions.status())
            if path == "/api/missions/history":
                try:
                    limit = int(qs.get("limit", ["20"])[0])
                except (TypeError, ValueError):
                    limit = 20
                rows = [] if missions is None else missions.history(limit)
                return self._json(200, {"runs": rows})
            if path == "/api/workouts/recent":
                code, payload = _workout_recent(workouts, qs)
                return self._json(code, payload)
            if path == "/api/workouts/summary":
                code, payload = _workout_summary(workouts, qs)
                return self._json(code, payload)
            if path == "/api/devices":
                cameras = []
                if vision is not None:
                    cameras = vision.camera_view().get("cameras") or []
                roarm = None
                snap = read_snapshot_file(snapshot_path, utcnow(), hard_stale)
                if isinstance(snap.get("roarm"), dict):
                    roarm = snap["roarm"]
                from perception.hub import devices_from
                devices = devices_from(cameras, roarm)
                if home is not None:
                    try:
                        view = home.entity_view()
                    except Exception:
                        view = {"available": False}
                    if isinstance(view, dict) and view.get("available") is True:
                        from home.adapter import devices_from_entities
                        devices.extend(devices_from_entities(view.get("entities") or []))
                return self._json(200, {"devices": devices})
            if path.startswith("/api/perception/stream/"):
                camera_id = path[len("/api/perception/stream/"):]
                if not camera_id or "/" in camera_id:
                    return self._json(404, {"accepted": False, "reason": "UNKNOWN_CAMERA"})
                if vision is None:
                    return self._json(503, {"accepted": False, "reason": "VISION_HUB_UNAVAILABLE"})
                upstream, reason = vision.open_stream(camera_id)
                if upstream is None:
                    code = 404 if reason == "UNKNOWN_CAMERA" else 400 if reason == "CAPABILITY_UNAVAILABLE" else 503
                    return self._json(code, {"accepted": False, "reason": reason})
                headers = getattr(upstream, "headers", None)
                content_type = ""
                if headers is not None and callable(getattr(headers, "get", None)):
                    content_type = headers.get("Content-Type") or ""
                self.send_response(200)
                self.send_header("Content-Type", content_type or "multipart/x-mixed-replace")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                try:
                    relay_stream(self.wfile, upstream)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
                    pass
                return
            if path.startswith("/api/perception/snapshot/"):
                if vision is None:
                    return self._json(503, {"accepted": False, "reason": "VISION_HUB_UNAVAILABLE"})
                camera_id = path.rsplit("/", 1)[-1]
                body, reason = vision.snapshot(camera_id)
                if body is None:
                    code = 404 if reason == "UNKNOWN_CAMERA" else 400 if reason == "CAPABILITY_UNAVAILABLE" else 503
                    return self._json(code, {"accepted": False, "reason": reason})
                return self._send(200, body, "image/jpeg")
            if path == "/api/perception/media":
                if vision is None:
                    return self._json(503, {"accepted": False, "reason": "VISION_HUB_UNAVAILABLE"})
                body, reason = vision.media((qs.get("path") or [""])[0])
                if body is None:
                    return self._json(400, {"accepted": False, "reason": reason})
                return self._send(200, body, "image/jpeg")
            if path == "/api/roarm/skills":
                if owner is None:
                    return self._json(503, {"accepted": False, "reason": "CONTROL_OWNER_UNAVAILABLE"})
                return self._json(200, owner.catalog())
            if path == "/api/health":
                summary = {
                    "available": False,
                    "state": "IDLE",
                    "reason": "MISSION_ENGINE_UNAVAILABLE",
                    "active_run_id": None,
                    "step": None,
                    "stop_requested": False,
                }
                if missions is not None:
                    try:
                        summary = missions.health_summary()
                    except Exception:
                        summary["reason"] = "MISSION_ENGINE_UNAVAILABLE"
                return self._json(200, {
                    "ok": True,
                    "role": "mission-control-ui",
                    "lan_only": True,
                    "state_dir": str(state_dir),
                    "hard_stale_s": hard_stale,
                    "mission_engine": summary,
                })
            if path == "/api/snapshot":
                snap = read_snapshot_file(snapshot_path, utcnow(), hard_stale)
                snap.setdefault("_ui", {})
                snap["_ui"].update({
                    "served_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "state_dir": str(state_dir),
                    "hard_stale_s": hard_stale,
                    "source": str(snapshot_path),
                })
                return self._json(200, snap)
            if path == "/api/events":
                try:
                    limit = int(qs.get("limit", [events_limit])[0])
                except (TypeError, ValueError):
                    limit = events_limit
                limit = max(0, min(limit, 500))
                events = tail_jsonl(events_path, limit)
                return self._json(200, {
                    "events": events,
                    "count": len(events),
                    "limit": limit,
                    "source": str(events_path),
                    "served_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                })
            return self._json(404, {"error": "not found", "path": path})

        def _static(self, rel: str):
            target = (ui_dir / rel).resolve()
            try:
                target.relative_to(ui_dir.resolve())
            except ValueError:
                return self._send(403, b"forbidden\n", "text/plain; charset=utf-8")
            if not target.is_file():
                return self._send(404, b"not found\n", "text/plain; charset=utf-8")
            data = target.read_bytes()
            ctype = _static_type(target)
            cache = "no-cache" if rel.endswith((".html", ".js", ".css", ".webmanifest")) else "public, max-age=60"
            return self._send(200, data, ctype, cache=cache)

    return Handler


def build_owner(state_dir: Path):
    """The Mission Control process is the only operational-state writer."""
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from control.owner import SkillOwner
    from control.production import ProductionCommandPort, readiness_from_snapshot
    from guardian.config import load_config
    from guardian.roarm import runtime_dir_from_config
    roarm = {}
    try:
        loaded = load_config(root / "config" / "services.yaml")
        if isinstance(loaded.get("roarm"), dict):
            roarm = loaded["roarm"]
    except (OSError, ValueError, KeyError, TypeError):
        roarm = {}
    workspace = roarm.get("workspace_mm") if isinstance(roarm.get("workspace_mm"), dict) else None
    command_root = roarm.get("command_root") or "/home/KA_PI/roarm-m3-pattern-cmd"
    snapshot = Path(state_dir) / "snapshot.json"
    return SkillOwner(
        Path(state_dir) / "operational-state.json",
        ProductionCommandPort(command_root, runtime_dir_from_config(roarm)),
        lambda: readiness_from_snapshot(snapshot),
        utcnow,
        workspace,
    )


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="SYZYGY Mission Control LAN UI and RoArm skill API")
    p.add_argument("--host", default=os.environ.get("MC_HOST", DEFAULT_HOST),
                   help="Bind address (default 127.0.0.1; use 0.0.0.0 for LAN). Do not publish this port directly.")
    p.add_argument("--port", type=int, default=int(os.environ.get("MC_PORT", DEFAULT_PORT)))
    p.add_argument("--state-dir", default=os.environ.get("MC_STATE_DIR", "state"),
                   help="Directory containing snapshot.json and events.jsonl")
    p.add_argument("--ui-dir", default=str(UI_DIR), help="Static UI asset directory")
    p.add_argument("--hard-stale-s", type=float,
                   default=float(os.environ.get("MC_HARD_STALE_S", DEFAULT_HARD_STALE_S)),
                   help="Guardian heartbeat hard-stale threshold seconds (default 120)")
    p.add_argument("--events-limit", type=int, default=DEFAULT_EVENTS_LIMIT)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    state_dir = Path(args.state_dir).expanduser().resolve()
    ui_dir = Path(args.ui_dir).expanduser().resolve()
    if not ui_dir.is_dir():
        sys.stderr.write(f"UI dir missing: {ui_dir}\n")
        return 2
    if args.host not in ("127.0.0.1", "localhost", "0.0.0.0") and not args.host.startswith("192.168."):
        sys.stderr.write(
            "WARNING: bind host %r looks unusual. Keep the origin on localhost or the LAN; "
            "do not publish port 9070 directly.\n" % (args.host,)
        )
    owner = build_owner(state_dir)
    root = Path(__file__).resolve().parent.parent
    vision = _vision_from_config(root)
    home = _home_from_config(root)
    missions = build_missions(state_dir, home, args.hard_stale_s)
    interval = 10.0
    try:
        from guardian.config import load_config
        loaded = load_config(Path(__file__).resolve().parent.parent / "config" / "services.yaml")
        owner_cfg = loaded.get("control_owner") if isinstance(loaded.get("control_owner"), dict) else {}
        interval = float(owner_cfg.get("heartbeat_s", interval))
    except (OSError, ValueError, KeyError, TypeError):
        interval = 10.0

    def heartbeat_loop():
        while True:
            owner.beat()
            time.sleep(interval)

    threading.Thread(target=heartbeat_loop, name="control-owner-heartbeat", daemon=True).start()
    handler = make_handler(
        state_dir, ui_dir, args.hard_stale_s, args.events_limit,
        owner, vision, home, missions, _workouts_from_env(),
    )
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    sys.stderr.write(
        "SYZYGY Mission Control V0.1 listening on http://%s:%d/\n"
        "  state-dir=%s\n"
        "  ui-dir=%s\n"
        "  hard-stale-s=%s\n"
        "  origin stays local — remote access is only the Access hostname\n"
        % (args.host, args.port, state_dir, ui_dir, args.hard_stale_s)
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\nshutting down\n")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
