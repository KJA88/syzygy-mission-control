#!/usr/bin/env python3
"""SYZYGY Mission Control V0.1 — LAN-only read-only UI server.

Serves static UI assets and Guardian outputs (snapshot.json + events.jsonl).
Does NOT probe MCP, hosts, Cloudflare, or any remote service.
Must remain LAN-only — never publish through Cloudflare.

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


def make_handler(state_dir: Path, ui_dir: Path, hard_stale: float, events_limit: int):
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
            if path == "/api/health":
                return self._json(200, {
                    "ok": True,
                    "role": "mission-control-ui",
                    "lan_only": True,
                    "state_dir": str(state_dir),
                    "hard_stale_s": hard_stale,
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
            ctype, _ = mimetypes.guess_type(str(target))
            if ctype is None:
                ctype = "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
                ctype = ctype + "; charset=utf-8"
            cache = "no-cache" if rel.endswith((".html", ".js", ".css")) else "public, max-age=60"
            return self._send(200, data, ctype, cache=cache)

    return Handler


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="SYZYGY Mission Control V0.1 LAN UI server (read-only)")
    p.add_argument("--host", default=os.environ.get("MC_HOST", DEFAULT_HOST),
                   help="Bind address (default 127.0.0.1; use 0.0.0.0 for LAN). NEVER Cloudflare.")
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
            "WARNING: bind host %r looks unusual. Mission Control must stay LAN-only; "
            "do not publish via Cloudflare.\n" % (args.host,)
        )
    handler = make_handler(state_dir, ui_dir, args.hard_stale_s, args.events_limit)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    sys.stderr.write(
        "SYZYGY Mission Control V0.1 listening on http://%s:%d/\n"
        "  state-dir=%s\n"
        "  ui-dir=%s\n"
        "  hard-stale-s=%s\n"
        "  LAN-only — do not expose via Cloudflare\n"
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
