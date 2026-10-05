"""Named RoArm skills, one motion authority, and the operational-state writer.

Callers request a skill. The gate accepts or rejects. An accepted motion skill
acquires authority, drives the existing State Engine, and calls the production
command port. There is no approval queue.
"""
from __future__ import annotations

import os
from pathlib import Path
from threading import Lock
from uuid import uuid4

from guardian.model import stamp
from guardian.state_engine import OperationalStateStore
from guardian.storage import atomic_json

SKILLS = ("move_to_pose", "return_home", "run_pattern", "stop", "clear")
PATTERNS = ("lissajous", "circle", "spiral")
ACTIVE = ("PREPARING", "MOVING")

DEFAULT_WORKSPACE = {
    "x_min": -500.0,
    "x_max": 500.0,
    "y_min": -500.0,
    "y_max": 500.0,
    "z_min": 0.0,
    "z_max": 700.0,
    "t_min": -3.2,
    "t_max": 3.2,
    "r_min": -3.2,
    "r_max": 3.2,
    "spd_min": 0.05,
    "spd_max": 5.0,
}


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class SkillOwner:
    def __init__(self, path, port, readiness, now, workspace=None, heartbeat_path=None):
        self.port = port
        self.readiness = readiness
        self.now = now
        self.workspace = dict(DEFAULT_WORKSPACE)
        if isinstance(workspace, dict):
            self.workspace.update(workspace)
        self.store = OperationalStateStore(path)
        self.heartbeat_path = Path(heartbeat_path) if heartbeat_path else Path(path).with_name("control-owner.json")
        self._lock = Lock()
        self._inflight = False
        self._stop_requested = False
        self._hold = None
        self.states = []
        self.recover()

    def recover(self):
        view = self.store.recover(self.now(), str(uuid4()))
        self._inflight = False
        self._stop_requested = False
        self.states = [view.get("state")]
        self.beat()
        return view

    def beat(self):
        """Prove this process is the live control owner. Does not change operational state."""
        atomic_json(self.heartbeat_path, {
            "schema_version": 1,
            "role": "control-owner",
            "heartbeat_at": stamp(self.now()),
            "pid": os.getpid(),
        })

    def view(self):
        return self.store.view()

    def acquire_hold(self, holder, reason):
        """Reserve the arm for a non-motion owner. Motion skills cannot start while it is held."""
        holder = _text(holder)
        reason = _text(reason) or "CONTROL_HELD"
        if not holder:
            return False, "AUTHORITY_REQUIRED"
        with self._lock:
            if self._hold is not None:
                return False, self._hold[1] or "CONTROL_HELD"
            if self._inflight or self.view().get("state") in ACTIVE:
                return False, "ARM_ACTIVE"
            self._hold = (holder, reason)
            return True, "HELD"

    def release_hold(self, holder):
        holder = _text(holder)
        with self._lock:
            if self._hold is None:
                return True
            if self._hold[0] != holder:
                return False
            self._hold = None
            return True

    def set_hold_reason(self, holder, reason):
        holder = _text(holder)
        reason = _text(reason) or "CONTROL_HELD"
        with self._lock:
            if self._hold is None or self._hold[0] != holder:
                return False
            self._hold = (holder, reason)
            return True

    def hold(self):
        with self._lock:
            if self._hold is None:
                return None
            return {"holder": self._hold[0], "reason": self._hold[1]}

    def catalog(self):
        return {
            "skills": list(SKILLS),
            "patterns": list(PATTERNS),
            "gripper": None,
            "gripper_reason": "NO_PRODUCTION_COMMAND",
            "workspace_mm": {
                "x": [self.workspace["x_min"], self.workspace["x_max"]],
                "y": [self.workspace["y_min"], self.workspace["y_max"]],
                "z": [self.workspace["z_min"], self.workspace["z_max"]],
            },
        }

    def request(self, skill, authority=None, operator=None, mission=None,
                trace_id=None, params=None):
        """Event and UI callers use this. No browser-specific arguments."""
        trace_id = _text(trace_id) or str(uuid4())
        params = params if isinstance(params, dict) else {}
        name = _text(skill)
        holder = _text(authority)
        if name == "stop":
            return self.stop(authority=holder, operator=operator, mission=mission, trace_id=trace_id)
        if name == "clear":
            return self.clear(operator=operator, trace_id=trace_id)
        with self._lock:
            rejection = self._gate(name, holder, params)
            if rejection:
                return self._result(False, "rejected", rejection, trace_id)
            self._begin(name, holder, operator, mission, trace_id, "SKILL_ACCEPTED")
        outcome = self._run(lambda: self.port.execute(name, params))
        with self._lock:
            return self._finish(name, outcome, trace_id)

    def stop(self, authority=None, operator=None, mission=None, trace_id=None):
        """Interrupt the active skill. Any caller may stop; this does not steal a new motion."""
        trace_id = _text(trace_id) or str(uuid4())
        with self._lock:
            if not self._inflight and self.view().get("state") not in ACTIVE:
                return self._result(False, "rejected", "NOT_ACTIVE", trace_id)
            self._stop_requested = True
            inflight = self._inflight
        if inflight:
            try:
                self.port.stop()
            except Exception:
                pass
            return self._result(True, "stop_requested", "STOP_REQUESTED", trace_id)
        with self._lock:
            self._transition("STOPPED", trace_id, "STOP", operator=operator, mission=mission)
            return self._result(True, "stopped", "STOP", trace_id)

    def clear(self, operator=None, trace_id=None):
        """Leave STOPPED or FAULT. Does not command the arm."""
        trace_id = _text(trace_id) or str(uuid4())
        with self._lock:
            if self._inflight or self.view().get("state") not in ("STOPPED", "FAULT"):
                return self._result(False, "rejected", "NOT_CLEARABLE", trace_id)
            self._transition("IDLE", trace_id, "CLEARED", operator=operator)
            return self._result(True, "cleared", "CLEARED", trace_id)

    def engineering(self, packet, authority=None, operator=None, mission=None, trace_id=None):
        """One raw HTTP packet for KJA. Same ownership rule as a skill."""
        trace_id = _text(trace_id) or str(uuid4())
        holder = _text(authority)
        if not isinstance(packet, dict) or not _number(packet.get("T")):
            return self._result(False, "rejected", "MALFORMED_PARAMETERS", trace_id)
        with self._lock:
            rejection = self._gate("engineering", holder, {})
            if rejection:
                return self._result(False, "rejected", rejection, trace_id)
            self._begin("engineering", holder, operator, mission, trace_id, "ENGINEERING_ACCEPTED")
        outcome = self._run(lambda: self.port.engineering(packet))
        with self._lock:
            return self._finish("engineering", outcome, trace_id)

    def _gate(self, name, authority, params):
        if name not in ("move_to_pose", "return_home", "run_pattern", "engineering"):
            return "UNKNOWN_SKILL"
        if not authority:
            return "AUTHORITY_REQUIRED"
        if name == "move_to_pose":
            malformed = self._pose_error(params)
            if malformed:
                return malformed
        elif name == "run_pattern" and params.get("pattern") not in PATTERNS:
            return "MALFORMED_PARAMETERS"
        if self._hold is not None:
            return self._hold[1] or "CONTROL_HELD"
        if self._inflight or self.view().get("state") in ACTIVE:
            return "AUTHORITY_HELD"
        state = self.view().get("state")
        if state == "FAULT":
            return "FAULT_NOT_CLEARED"
        if state == "STOPPED":
            return "STOPPED_NOT_CLEARED"
        ready = self.readiness() if self.readiness else {}
        if not isinstance(ready, dict):
            ready = {}
        if ready.get("reachable") is not True or ready.get("fresh") is not True:
            return "ARM_NOT_READY"
        available = getattr(self.port, "available", lambda: True)
        if available() is not True:
            return "COMMAND_PATH_UNAVAILABLE"
        return None

    def _pose_error(self, params):
        if not isinstance(params, dict):
            return "MALFORMED_PARAMETERS"
        for key in ("x", "y", "z"):
            if not _number(params.get(key)):
                return "MALFORMED_PARAMETERS"
        optional = {"t": ("t_min", "t_max"), "r": ("r_min", "r_max"), "spd": ("spd_min", "spd_max")}
        bounds = {
            "x": ("x_min", "x_max"),
            "y": ("y_min", "y_max"),
            "z": ("z_min", "z_max"),
        }
        for key, pair in bounds.items():
            if not self._inside(params[key], pair):
                return "OUTSIDE_WORKSPACE"
        for key, pair in optional.items():
            if key not in params or params[key] is None:
                continue
            if not _number(params[key]) or not self._inside(params[key], pair):
                return "MALFORMED_PARAMETERS" if not _number(params[key]) else "OUTSIDE_WORKSPACE"
        return None

    def _inside(self, value, pair):
        low = self.workspace[pair[0]]
        high = self.workspace[pair[1]]
        return low <= float(value) <= high

    def _begin(self, skill, authority, operator, mission, trace_id, reason):
        self._stop_requested = False
        self._transition(
            "PREPARING",
            trace_id,
            reason,
            operator=operator,
            skill=skill,
            mission=mission,
            motion_authority=authority,
        )
        self._transition("MOVING", trace_id, "SKILL_STARTED", motion_authority=authority)
        self._inflight = True

    def _run(self, call):
        try:
            if self._stop_requested:
                return {"ok": False, "stopped": True, "reason": "PATTERN_STOP_REQUESTED"}
            outcome = call()
        except Exception as exc:
            outcome = {"ok": False, "reason": "SKILL_FAILED", "error": str(exc)}
        if not isinstance(outcome, dict):
            outcome = {"ok": False, "reason": "SKILL_FAILED"}
        return outcome

    def _finish(self, skill, outcome, trace_id):
        self._inflight = False
        stopped = outcome.get("stopped") is True or outcome.get("reason") == "PATTERN_STOP_REQUESTED" or self._stop_requested
        if stopped:
            self._transition("STOPPED", trace_id, "STOP")
            return self._result(True, "stopped", "STOP", trace_id)
        if outcome.get("ok") is True:
            self._transition("COMPLETE", trace_id, "SKILL_FINISHED")
            self._transition("IDLE", trace_id, "SKILL_RELEASED", operator=None)
            return self._result(True, "completed", "SKILL_FINISHED", trace_id)
        reason = _text(outcome.get("reason")) or "SKILL_FAILED"
        self._transition(
            "FAULT",
            trace_id,
            "SKILL_FAILED",
            fault_class=reason,
            fault_reason=_text(outcome.get("error")) or reason,
        )
        return self._result(False, "failed", reason, trace_id)

    def _transition(self, target, trace_id, reason, **fields):
        view, accepted = self.store.transition(
            target,
            now=self.now(),
            trace_id=trace_id,
            reason=reason,
            **fields,
        )
        if not accepted:
            raise RuntimeError(view.get("last_rejection"))
        self.states.append(view.get("state"))
        self.beat()
        return view

    def _result(self, accepted, result, reason, trace_id):
        view = self.view()
        return {
            "accepted": accepted,
            "result": result,
            "reason": reason,
            "trace_id": trace_id,
            "state": view.get("state"),
            "motion_authority": view.get("motion_authority"),
            "skill": view.get("skill"),
            "last_completed_action": view.get("last_completed_action"),
            "operational": view,
        }
