"""Call the existing RoArm command layer. This module does not speak UDP or HTTP itself."""
from __future__ import annotations

import json
import sys
from pathlib import Path


def readiness_from_snapshot(path):
    """Guardian already probes T105. The skill owner does not issue another probe."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"reachable": False, "fresh": False}
    roarm = raw.get("roarm") if isinstance(raw, dict) else None
    if not isinstance(roarm, dict):
        return {"reachable": False, "fresh": False}
    return {
        "reachable": roarm.get("reachability") == "reachable" and roarm.get("connected") is True,
        "fresh": roarm.get("fresh") is True and roarm.get("t105_fresh") is True,
    }


def _normalize(outcome):
    if not isinstance(outcome, dict):
        return {"ok": False, "reason": "SKILL_FAILED"}
    reason = outcome.get("reason")
    stopped = outcome.get("stopped") is True or reason == "PATTERN_STOP_REQUESTED"
    return {
        "ok": outcome.get("ok") is True and not stopped,
        "stopped": stopped,
        "reason": reason or ("PATTERN_FINISHED" if outcome.get("ok") is True else "SKILL_FAILED"),
        "error": outcome.get("error"),
    }


class ProductionCommandPort:
    """Imports runtime.core.safety.skill_adapter from the production command checkout."""

    def __init__(self, command_root):
        self.command_root = str(command_root)

    def available(self):
        root = Path(self.command_root)
        return (root / "runtime" / "core" / "safety" / "skill_adapter.py").is_file()

    def _adapter(self):
        root = self.command_root
        if root not in sys.path:
            sys.path.insert(0, root)
        from runtime.core.safety import skill_adapter
        return skill_adapter

    def execute(self, skill, params):
        adapter = self._adapter()
        if skill == "move_to_pose":
            return _normalize(adapter.move_pose(params))
        if skill == "return_home":
            return _normalize(adapter.run_named("home"))
        if skill == "run_pattern":
            return _normalize(adapter.run_named(params["pattern"]))
        if skill == "stop":
            return _normalize(adapter.stop_motion())
        return {"ok": False, "reason": "UNKNOWN_SKILL"}

    def stop(self):
        return _normalize(self._adapter().stop_motion())

    def engineering(self, packet):
        return _normalize(self._adapter().engineering(packet))
