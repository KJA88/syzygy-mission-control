"""Load data-driven switch-cycle missions. There is no executable step language."""
from __future__ import annotations

import re

import yaml

from home.adapter import domain_of, entity_id_of

MISSION_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ALLOWED = frozenset({
    "id",
    "name",
    "version",
    "kind",
    "physical",
    "subsystem",
    "target",
    "final_state",
    "wait_s",
    "observe_timeout_s",
})
FORBIDDEN = frozenset({
    "command",
    "shell",
    "url",
    "service",
    "call_service",
    "code",
    "packet",
    "script",
    "executable",
    "http",
    "udp",
})


class MissionDefinition:
    def __init__(self, data):
        self.id = data["id"]
        self.name = data["name"]
        self.version = data["version"]
        self.kind = data["kind"]
        self.physical = True
        self.subsystem = data["subsystem"]
        self.target = data["target"]
        self.final_state = data["final_state"]
        self.wait_s = float(data["wait_s"])
        self.observe_timeout_s = float(data["observe_timeout_s"])

    def public(self):
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "kind": self.kind,
            "physical": self.physical,
            "subsystem": self.subsystem,
            "target": self.target,
            "final_state": self.final_state,
            "wait_s": self.wait_s,
            "observe_timeout_s": self.observe_timeout_s,
        }


def _number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value <= low or value > high:
        return None
    return float(value)


def _one(raw):
    if not isinstance(raw, dict):
        raise ValueError("MISSION_DEFINITION_INVALID")
    if FORBIDDEN.intersection(raw) or not set(raw).issubset(ALLOWED):
        raise ValueError("MISSION_DEFINITION_INVALID")
    ident = raw.get("id")
    name = raw.get("name")
    if not isinstance(ident, str) or not MISSION_ID.match(ident):
        raise ValueError("MISSION_DEFINITION_INVALID")
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise ValueError("MISSION_DEFINITION_INVALID")
    version = raw.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ValueError("MISSION_DEFINITION_INVALID")
    if raw.get("kind") != "switch_cycle" or raw.get("physical") is not True:
        raise ValueError("MISSION_DEFINITION_INVALID")
    if raw.get("subsystem") != "home_assistant":
        raise ValueError("MISSION_DEFINITION_INVALID")
    target = entity_id_of(raw.get("target"))
    if target is None or domain_of(target) not in {"switch", "light"}:
        raise ValueError("MISSION_DEFINITION_INVALID")
    final_state = raw.get("final_state")
    if final_state not in {"off", "on"}:
        raise ValueError("MISSION_DEFINITION_INVALID")
    wait_s = _number(raw.get("wait_s"), 0, 30)
    observe = _number(raw.get("observe_timeout_s"), 0, 10)
    if wait_s is None or observe is None:
        raise ValueError("MISSION_DEFINITION_INVALID")
    cleaned = dict(raw)
    cleaned["id"] = ident
    cleaned["name"] = name.strip()
    cleaned["target"] = target
    cleaned["wait_s"] = wait_s
    cleaned["observe_timeout_s"] = observe
    return MissionDefinition(cleaned)


def load_definitions(path):
    """Return {id: MissionDefinition}. Raise ValueError with a stable reason."""
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError("MISSION_DEFINITION_INVALID") from exc
    if not isinstance(loaded, dict) or not isinstance(loaded.get("missions"), list):
        raise ValueError("MISSION_DEFINITION_INVALID")
    found = {}
    for raw in loaded["missions"]:
        definition = _one(raw)
        if definition.id in found:
            raise ValueError("MISSION_DEFINITION_INVALID")
        found[definition.id] = definition
    if not found:
        raise ValueError("MISSIONS_UNCONFIGURED")
    return found
