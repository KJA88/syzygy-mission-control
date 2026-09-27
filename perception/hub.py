"""Normalize Vision Hub cameras and dispatch operator actions.

The browser talks to Mission Control. This module talks to the existing
Vision Hub HTTP API. It does not run YOLO or move PTZ by itself.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

SECRET_FIELDS = frozenset({
    "rtsp_url",
    "ptz_user",
    "ptz_pass",
    "password",
    "token",
    "secret",
})
PTZ_DIRECTIONS = frozenset({"left", "right", "up", "down", "stop"})
SAFE_ID = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def _text(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def safe_camera_id(value):
    text = _text(value)
    if not text or any(char not in SAFE_ID for char in text):
        return None
    return text


def capabilities_for(camera):
    """Derive capabilities from camera metadata. Never from a camera name.

    Inference still runs when monitor_only is set; that flag only suppresses
    actuation. The manual current-frame snapshot is separate from the
    automatic detection-snapshot flag.
    """
    if not isinstance(camera, dict):
        return []
    found = ["stream", "detect", "snapshot"]
    if camera.get("type") == "ptz":
        found.extend(["ptz", "track"])
    return found


def _parent_id(metadata, camera_id):
    item = metadata.get(camera_id) if isinstance(metadata, dict) else None
    if not isinstance(item, dict):
        return None
    return _text(item.get("parent_id"))


def _last_event(events, camera_id):
    if not isinstance(events, list):
        return None
    for event in events:
        if isinstance(event, dict) and event.get("camera") == camera_id:
            return {
                "camera": camera_id,
                "class": _text(event.get("class")),
                "confidence": event.get("confidence") if isinstance(event.get("confidence"), (int, float)) and not isinstance(event.get("confidence"), bool) else None,
                "timestamp": _text(event.get("timestamp")),
                "image": _text(event.get("image")) if _safe_media_path(event.get("image")) else None,
            }
    return None


def _safe_media_path(value):
    text = _text(value)
    if not text or text.startswith("/") or "\\" in text or ".." in text.split("/"):
        return None
    parts = text.split("/")
    if len(parts) < 2 or parts[0] != "detections":
        return None
    if not text.endswith(".jpg"):
        return None
    return text


def normalize_cameras(config, status, events, *, service_base, hub_ui, metadata=None):
    """Return operator cameras. Secret fields are dropped."""
    if not isinstance(config, dict) or not isinstance(config.get("cameras"), dict):
        return {"available": False, "reason": "VISION_HUB_UNAVAILABLE", "cameras": [], "hub_url": hub_ui}
    status = status if isinstance(status, dict) else {}
    metadata = metadata if isinstance(metadata, dict) else {}
    service = str(service_base or "").rstrip("/")
    cameras = []
    for camera_id, raw in config["cameras"].items():
        ident = safe_camera_id(camera_id)
        if ident is None or not isinstance(raw, dict):
            continue
        item_status = status.get(ident) if isinstance(status.get(ident), dict) else {}
        if "online" in item_status:
            online = item_status.get("online") is True
            health = "online" if online else "offline"
        else:
            online = None
            health = "unknown"
        mode = _text(item_status.get("mode"))
        caps = capabilities_for(raw)
        cameras.append({
            "id": ident,
            "name": _text(raw.get("name")) or ident,
            "kind": "camera",
            "provider": "vision-hub",
            "provider_id": ident,
            "parent_id": _parent_id(metadata, ident),
            "type": _text(raw.get("type")) or "unknown",
            "online": online,
            "health": health,
            "mode": mode,
            "capabilities": caps,
            "tracking": raw.get("tracking") is True if "track" in caps else None,
            "stream_url": service + "/stream/" + ident if service else None,
            "hub_url": hub_ui,
            "last_event": _last_event(events, ident),
        })
    return {"available": True, "reason": None, "cameras": cameras, "hub_url": hub_ui}


def devices_from(cameras, roarm=None):
    """Current devices only: discovered cameras plus the existing RoArm."""
    items = []
    for camera in cameras if isinstance(cameras, list) else []:
        if not isinstance(camera, dict):
            continue
        items.append({
            "id": "camera:" + str(camera.get("id")),
            "display_name": camera.get("name"),
            "kind": "camera",
            "provider": "vision-hub",
            "provider_id": camera.get("id"),
            "parent_id": camera.get("parent_id"),
            "capabilities": list(camera.get("capabilities") or []),
            "health": camera.get("health") or "unknown",
            "status": camera.get("mode"),
        })
    arm_health = "unknown"
    arm_status = None
    if isinstance(roarm, dict):
        arm_status = _text(roarm.get("status"))
        if roarm.get("reachability") == "reachable" and roarm.get("connected") is True:
            arm_health = "online"
        elif roarm.get("reachability") == "unreachable" or roarm.get("connected") is False:
            arm_health = "offline"
    items.append({
        "id": "arm:roarm-1",
        "display_name": "RoArm-M3",
        "kind": "arm",
        "provider": "mission-control",
        "provider_id": "roarm",
        "parent_id": None,
        "capabilities": ["move_to_pose", "return_home", "run_pattern", "stop"],
        "health": arm_health,
        "status": arm_status,
    })
    return items


def public_events(events, limit=50):
    if not isinstance(events, list):
        return []
    cleaned = []
    for event in events[:limit]:
        if not isinstance(event, dict):
            continue
        cleaned.append({
            "camera": _text(event.get("camera")),
            "class": _text(event.get("class")),
            "confidence": event.get("confidence") if isinstance(event.get("confidence"), (int, float)) and not isinstance(event.get("confidence"), bool) else None,
            "timestamp": _text(event.get("timestamp")),
            "image": _safe_media_path(event.get("image")),
        })
    return cleaned


def public_gallery(images, limit=40):
    if not isinstance(images, list):
        return []
    cleaned = []
    for image in images[:limit]:
        if not isinstance(image, dict):
            continue
        path = _safe_media_path(image.get("path"))
        if path is None:
            continue
        cleaned.append({
            "camera": _text(image.get("camera")),
            "name": _text(image.get("name")),
            "path": path,
            "timestamp": _text(image.get("ts")),
        })
    return cleaned


class VisionHub:
    def __init__(self, hub_base, service_base, hub_ui=None, metadata=None, opener=None, timeout=3.0):
        self.hub_base = str(hub_base or "").rstrip("/")
        self.service_base = str(service_base or "").rstrip("/")
        self.hub_ui = hub_ui or (self.hub_base + "/" if self.hub_base else None)
        self.metadata = metadata if isinstance(metadata, dict) else {}
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout
        self._camera_cache = None

    def _request(self, url, method="GET", payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self.opener(request, timeout=self.timeout) as response:
                body = response.read()
                header = response.headers.get("Content-Type", "") if getattr(response, "headers", None) else ""
                status = getattr(response, "status", 200)
                return status, header, body, None
        except urllib.error.HTTPError as exc:
            detail = exc.read() if hasattr(exc, "read") else b""
            return exc.code, "", detail, "VISION_HUB_HTTP"
        except Exception:
            return None, "", b"", "VISION_HUB_UNAVAILABLE"

    def _json(self, url):
        status, _header, body, error = self._request(url)
        if error or status is None or status >= 400:
            return None, error or "VISION_HUB_UNAVAILABLE"
        try:
            return json.loads(body.decode("utf-8")), None
        except (UnicodeError, ValueError):
            return None, "VISION_HUB_MALFORMED"

    def camera_view(self):
        if not self.hub_base:
            return {"available": False, "reason": "VISION_HUB_UNAVAILABLE", "cameras": [], "hub_url": self.hub_ui}
        config, error = self._json(self.hub_base + "/api/config")
        if error:
            return {"available": False, "reason": error, "cameras": [], "hub_url": self.hub_ui}
        self._remember_cameras(config)
        status, _status_error = self._json(self.hub_base + "/api/cameras/status")
        events, _event_error = self._json(self.hub_base + "/api/events?limit=50")
        return normalize_cameras(
            config,
            status,
            events,
            service_base=self.service_base,
            hub_ui=self.hub_ui,
            metadata=self.metadata,
        )

    def events(self, limit=50):
        payload, error = self._json(self.hub_base + "/api/events?limit=%d" % int(limit))
        if error:
            return {"available": False, "reason": error, "events": []}
        return {"available": True, "reason": None, "events": public_events(payload, limit)}

    def snapshots(self, camera_id=None, limit=40):
        url = self.hub_base + "/api/gallery?limit=%d" % int(limit)
        ident = safe_camera_id(camera_id) if camera_id else None
        if ident:
            url += "&camera=" + ident
        payload, error = self._json(url)
        if error:
            return {"available": False, "reason": error, "snapshots": []}
        return {"available": True, "reason": None, "snapshots": public_gallery(payload, limit)}

    def _remember_cameras(self, config):
        if isinstance(config, dict) and isinstance(config.get("cameras"), dict):
            self._camera_cache = config["cameras"]
            return self._camera_cache
        return None

    def _config_cameras(self):
        """Capability data only. Status and events are not part of command validation."""
        if not self.hub_base:
            return None, "VISION_HUB_UNAVAILABLE"
        config, error = self._json(self.hub_base + "/api/config")
        if error:
            return None, error
        cameras = self._remember_cameras(config)
        if cameras is None:
            return None, "VISION_HUB_MALFORMED"
        return cameras, None

    def _known_camera(self, camera_id):
        ident = safe_camera_id(camera_id)
        if ident is None:
            return None, "UNKNOWN_CAMERA"
        cameras = self._camera_cache if isinstance(self._camera_cache, dict) else None
        if cameras is None or ident not in cameras:
            cameras, error = self._config_cameras()
            if error:
                return None, error
        raw = cameras.get(ident) if isinstance(cameras, dict) else None
        if not isinstance(raw, dict):
            return None, "UNKNOWN_CAMERA"
        return {"id": ident, "capabilities": capabilities_for(raw)}, None

    def track(self, camera_id, enabled):
        camera, reason = self._known_camera(camera_id)
        if camera is None:
            return {"accepted": False, "reason": reason}
        if "track" not in camera["capabilities"]:
            return {"accepted": False, "reason": "CAPABILITY_UNAVAILABLE"}
        if not isinstance(enabled, bool):
            return {"accepted": False, "reason": "MALFORMED_PARAMETERS"}
        status, _header, body, error = self._request(
            self.hub_base + "/api/config/" + camera["id"],
            method="POST",
            payload={"tracking": enabled},
        )
        if error or status is None or status >= 400:
            return {"accepted": False, "reason": error or "VISION_HUB_HTTP"}
        try:
            parsed = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeError, ValueError):
            parsed = {}
        if isinstance(parsed, dict) and parsed.get("ok") is False:
            return {"accepted": False, "reason": "VISION_HUB_HTTP"}
        return {"accepted": True, "reason": "TRACK_UPDATED", "camera": camera["id"], "tracking": enabled}

    def _dispatch_ptz(self, ident, direction):
        status, _header, _body, error = self._request(
            self.hub_base + "/ptz/" + ident,
            method="POST",
            payload={"dir": direction, "action": "stop" if direction == "stop" else "start"},
        )
        if error or status is None or status >= 400:
            return {"accepted": False, "reason": error or "VISION_HUB_HTTP"}
        return {"accepted": True, "reason": "PTZ_SENT", "camera": ident, "dir": direction}

    def ptz(self, camera_id, direction):
        direction = _text(direction)
        if direction not in PTZ_DIRECTIONS:
            return {"accepted": False, "reason": "MALFORMED_PARAMETERS"}
        camera, reason = self._known_camera(camera_id)
        if camera is None:
            return {"accepted": False, "reason": reason}
        if "ptz" not in camera["capabilities"]:
            return {"accepted": False, "reason": "CAPABILITY_UNAVAILABLE"}
        return self._dispatch_ptz(camera["id"], direction)

    def snapshot(self, camera_id):
        camera, reason = self._known_camera(camera_id)
        if camera is None:
            return None, reason
        if "snapshot" not in camera["capabilities"]:
            return None, "CAPABILITY_UNAVAILABLE"
        status, header, body, error = self._request(self.service_base + "/snapshot/" + camera["id"])
        if error or status is None or status >= 400 or not body:
            return None, error or "SNAPSHOT_UNAVAILABLE"
        if "jpeg" not in header.lower() and not body.startswith(b"\xff\xd8"):
            return None, "SNAPSHOT_UNAVAILABLE"
        return body, None

    def media(self, path):
        safe = _safe_media_path(path)
        if safe is None:
            return None, "MALFORMED_PARAMETERS"
        status, header, body, error = self._request(self.hub_base + "/snapshots/" + safe)
        if error or status is None or status >= 400 or not body:
            return None, error or "SNAPSHOT_UNAVAILABLE"
        if body.startswith(b"\xff\xd8") or "jpeg" in header.lower() or "image" in header.lower():
            return body, None
        return None, "SNAPSHOT_UNAVAILABLE"
