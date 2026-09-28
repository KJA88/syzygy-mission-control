"""Normalize Home Assistant entities and allow only explicit low-risk writes.

The token never leaves this process. Callers receive fixed reason codes.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
READ_DOMAINS = frozenset({"sensor", "binary_sensor", "switch", "light"})
WRITE_DOMAINS = frozenset({"switch", "light"})
REJECTED_DOMAINS = frozenset({
    "lock",
    "alarm_control_panel",
    "cover",
    "climate",
    "water_heater",
    "humidifier",
    "script",
    "automation",
    "scene",
    "vacuum",
    "camera",
    "media_player",
})
ACTIONS = frozenset({"turn_on", "turn_off", "set_brightness"})


def _text(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def entity_id_of(value):
    text = _text(value)
    if not text or not ENTITY_ID.match(text):
        return None
    return text


def domain_of(entity_id):
    ident = entity_id_of(entity_id)
    if ident is None:
        return None
    return ident.split(".", 1)[0]


def policy_from_config(raw, environ=None):
    """Build policy. The token is read from the environment, never from YAML."""
    raw = raw if isinstance(raw, dict) else {}
    env = environ if environ is not None else os.environ
    read = raw.get("read_domains")
    write = raw.get("write_domains")
    allow = raw.get("write_allow")
    exclude = raw.get("exclude")
    include = raw.get("include")
    return {
        "base_url": _text(env.get("HA_BASE_URL")) or "",
        "token": _text(env.get("HA_TOKEN")) or "",
        "ui_url": _text(raw.get("ui_url")),
        "read_domains": _domain_set(read, READ_DOMAINS),
        "write_domains": _domain_set(write, WRITE_DOMAINS) & WRITE_DOMAINS,
        "write_allow": _id_set(allow),
        "exclude": _id_set(exclude),
        "include": _id_set(include),
        "timeout": _timeout(raw.get("timeout_s")),
    }


def _timeout(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 4.0
    if number <= 0 or number > 10:
        return 4.0
    return number


def _domain_set(value, default):
    if not isinstance(value, list) or not value:
        return set(default)
    found = set()
    for item in value:
        text = _text(item)
        if text:
            found.add(text)
    return found or set(default)


def _id_set(value):
    if not isinstance(value, list):
        return set()
    found = set()
    for item in value:
        ident = entity_id_of(item)
        if ident:
            found.add(ident)
    return found


def _brightness_supported(attributes):
    modes = attributes.get("supported_color_modes")
    if isinstance(modes, list) and "brightness" in modes:
        return True
    features = attributes.get("supported_features")
    return isinstance(features, int) and not isinstance(features, bool) and bool(features & 1)


def normalize_entity(raw, policy):
    """Return one operator entity, or None when it should be omitted."""
    if not isinstance(raw, dict):
        return None
    ident = entity_id_of(raw.get("entity_id"))
    if ident is None:
        return None
    domain = domain_of(ident)
    if ident in policy["exclude"] or domain not in policy["read_domains"]:
        return None
    if policy["include"] and ident not in policy["include"]:
        return None
    if domain in REJECTED_DOMAINS:
        return None
    attributes = raw.get("attributes") if isinstance(raw.get("attributes"), dict) else {}
    state = _text(raw.get("state")) or "unknown"
    available = state not in {"unavailable", "unknown"}
    writable = domain in policy["write_domains"] and ident in policy["write_allow"]
    capabilities = ["read"]
    if writable and domain == "switch":
        capabilities.extend(["turn_on", "turn_off"])
    elif writable and domain == "light":
        capabilities.extend(["turn_on", "turn_off"])
        if _brightness_supported(attributes):
            capabilities.append("brightness")
    name = _text(attributes.get("friendly_name")) or ident
    unit = _text(attributes.get("unit_of_measurement"))
    device_class = _text(attributes.get("device_class"))
    area = _text(attributes.get("area"))
    return {
        "id": ident,
        "entity_id": ident,
        "name": name,
        "domain": domain,
        "kind": domain,
        "state": state,
        "available": available,
        "readable": True,
        "writable": writable,
        "capabilities": capabilities,
        "unit": unit,
        "device_class": device_class,
        "last_changed": _text(raw.get("last_changed")) or _text(raw.get("last_updated")),
        "area": area,
        "provider": "home_assistant",
    }


def devices_from_entities(entities):
    items = []
    for entity in entities if isinstance(entities, list) else []:
        if not isinstance(entity, dict) or not entity.get("entity_id"):
            continue
        items.append({
            "id": "home:" + entity["entity_id"],
            "display_name": entity.get("name"),
            "kind": entity.get("domain"),
            "provider": "home_assistant",
            "provider_id": entity["entity_id"],
            "parent_id": entity.get("area"),
            "capabilities": list(entity.get("capabilities") or []),
            "health": "online" if entity.get("available") else "offline",
            "status": entity.get("state"),
        })
    return items


class HomeAssistant:
    def __init__(self, policy, opener=None, timeout=4.0):
        self.policy = policy
        self.base_url = str(policy.get("base_url") or "").rstrip("/")
        self.token = str(policy.get("token") or "")
        self.ui_url = policy.get("ui_url")
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout

    def _ui(self):
        return self.ui_url or self.base_url or None

    def _request(self, path, method="GET", payload=None):
        if not self.base_url:
            return None, b"", "HA_UNCONFIGURED"
        if not self.token:
            return None, b"", "HA_AUTH"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.base_url + path, data=data, method=method)
        request.add_header("Authorization", "Bearer " + self.token)
        request.add_header("Content-Type", "application/json")
        try:
            with self.opener(request, timeout=self.timeout) as response:
                status = getattr(response, "status", 200)
                body = response.read()
                if status >= 400:
                    return status, b"", "HA_HTTP"
                return status, body, None
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code in (401, 403):
                return exc.code, b"", "HA_AUTH"
            return exc.code, b"", "HA_HTTP"
        except TimeoutError:
            return None, b"", "HA_TIMEOUT"
        except Exception as exc:
            if exc.__class__.__name__ in {"TimeoutError", "URLError"} and "timed out" in str(exc).lower():
                return None, b"", "HA_TIMEOUT"
            return None, b"", "HA_UNAVAILABLE"

    def _states(self):
        status, body, error = self._request("/api/states")
        if error:
            return None, error
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeError, ValueError):
            return None, "HA_MALFORMED"
        if not isinstance(parsed, list):
            return None, "HA_MALFORMED"
        return parsed, None

    def entity_view(self):
        states, error = self._states()
        if error:
            return self._down(error)
        entities = []
        for raw in states:
            try:
                entity = normalize_entity(raw, self.policy)
            except Exception:
                entity = None
            if entity is not None:
                entities.append(entity)
        counts = {}
        for entity in entities:
            counts[entity["domain"]] = counts.get(entity["domain"], 0) + 1
        return {
            "available": True,
            "reason": None,
            "ui_url": self._ui(),
            "counts": counts,
            "entities": entities,
        }

    def status(self):
        view = self.entity_view()
        return {
            "available": view.get("available") is True,
            "reason": view.get("reason"),
            "ui_url": self._ui(),
            "counts": view.get("counts") or {},
        }

    def _down(self, reason):
        return {
            "available": False,
            "reason": reason,
            "ui_url": self._ui(),
            "counts": {},
            "entities": [],
        }

    def _known(self, entity_id):
        ident = entity_id_of(entity_id)
        if ident is None:
            return None, "MALFORMED_PARAMETERS"
        view = self.entity_view()
        if not view.get("available"):
            return None, view.get("reason") or "HA_UNAVAILABLE"
        for entity in view["entities"]:
            if entity["entity_id"] == ident:
                return entity, None
        domain = domain_of(ident)
        if domain in REJECTED_DOMAINS or domain not in self.policy["read_domains"]:
            return None, "DOMAIN_REJECTED"
        return None, "UNKNOWN_ENTITY"

    def act(self, entity_id, action, brightness=None):
        action = _text(action)
        if action not in ACTIONS:
            return {"accepted": False, "reason": "MALFORMED_PARAMETERS"}
        entity, reason = self._known(entity_id)
        if entity is None:
            return {"accepted": False, "reason": reason}
        needed = "brightness" if action == "set_brightness" else action
        if needed not in entity["capabilities"]:
            if entity["domain"] in REJECTED_DOMAINS or not entity["writable"]:
                return {"accepted": False, "reason": "POLICY_DENIED"}
            return {"accepted": False, "reason": "CAPABILITY_UNAVAILABLE"}
        if not entity["available"]:
            return {"accepted": False, "reason": "HA_UNAVAILABLE"}
        payload = {"entity_id": entity["entity_id"]}
        service = action
        if action == "set_brightness":
            if isinstance(brightness, bool) or not isinstance(brightness, int) or brightness < 0 or brightness > 255:
                return {"accepted": False, "reason": "MALFORMED_PARAMETERS"}
            service = "turn_on"
            payload["brightness"] = brightness
        status, _body, error = self._request(
            "/api/services/" + entity["domain"] + "/" + service,
            method="POST",
            payload=payload,
        )
        if error or status is None or status >= 400:
            return {"accepted": False, "reason": error or "HA_HTTP"}
        return {
            "accepted": True,
            "reason": "ACTION_ACCEPTED",
            "entity_id": entity["entity_id"],
            "action": action,
        }
