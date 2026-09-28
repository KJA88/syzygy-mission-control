"""One active mission at a time. Physical steps go through the Home Assistant adapter."""
from __future__ import annotations

import copy
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

from missions.definitions import load_definitions
from missions.store import RunStore
from missions.triggers import operator_trigger, validate_start_trigger

TERMINAL = frozenset({"COMPLETE", "FAULT", "STOPPED"})
SLICE_S = 0.05


def _stamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class MissionEngine:
    def __init__(self, definitions, store, home, health, clock=None, sleep=None,
                 synchronous=False, before_step=None, available=True, reason=None):
        self.definitions = definitions if isinstance(definitions, dict) else {}
        self.store = store
        self.home = home
        self.health = health
        self.clock = clock or time.monotonic
        self.sleeper = sleep or time.sleep
        self.synchronous = synchronous
        self.before_step = before_step
        self._available = available and bool(self.definitions)
        self._reason = reason if not self._available else None
        if not self.definitions and self._reason is None:
            self._reason = "MISSIONS_UNCONFIGURED"
        self._active = None
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()

    def list_definitions(self):
        return [item.public() for item in self.definitions.values()]

    def status(self):
        with self._lock:
            active = copy.deepcopy(self._active) if self._active else None
        return {
            "available": self._available,
            "reason": self._reason,
            "state": active["state"] if active else "IDLE",
            "stop_requested": bool(active) and self._stop.is_set(),
            "active": active,
        }

    def health_summary(self):
        current = self.status()
        active = current.get("active") or {}
        return {
            "available": current["available"],
            "state": current["state"],
            "reason": current["reason"],
            "active_run_id": active.get("run_id"),
            "step": active.get("step"),
            "stop_requested": current["stop_requested"],
        }

    def history(self, limit=20):
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 20
        limit = max(0, min(limit, 40))
        with self._lock:
            rows = copy.deepcopy(self.store.runs[:limit])
        return rows

    def start(self, mission_id, trigger=None, operator=None):
        if not self._available:
            return {"accepted": False, "reason": self._reason or "MISSION_ENGINE_UNAVAILABLE"}
        if not isinstance(mission_id, str):
            return {"accepted": False, "reason": "MALFORMED_PARAMETERS"}
        definition = self.definitions.get(mission_id)
        if definition is None:
            return {"accepted": False, "reason": "UNKNOWN_MISSION"}
        if trigger is None and operator is not None:
            trigger = operator_trigger(operator)
        accepted, reason = validate_start_trigger(trigger)
        if reason:
            return {"accepted": False, "reason": reason}
        with self._lock:
            if self._active is not None:
                return {"accepted": False, "reason": "OWNER_BUSY"}
            now = _stamp()
            run = {
                "run_id": uuid4().hex,
                "mission_id": definition.id,
                "name": definition.name,
                "version": definition.version,
                "target": definition.target,
                "final_state": definition.final_state,
                "trigger": accepted,
                "state": "TRIGGERED",
                "step": None,
                "started_at": now,
                "updated_at": now,
                "completed_at": None,
                "checks": [],
                "actions": [],
                "reason": None,
                "physical": True,
                "energized": False,
            }
            self._stop.clear()
            self._active = run
            self.store.add(run)
        if self.synchronous:
            self._execute(run)
        else:
            self._thread = threading.Thread(
                target=self._execute, args=(run,), name="mission-engine", daemon=True)
            self._thread.start()
        return {
            "accepted": True,
            "reason": None,
            "run_id": run["run_id"],
            "state": run["state"],
        }

    def stop(self):
        with self._lock:
            if self._active is None:
                return {"accepted": False, "reason": "NO_ACTIVE_MISSION"}
            self._stop.set()
            run_id = self._active.get("run_id")
        return {"accepted": True, "reason": "STOP_REQUESTED", "run_id": run_id}

    def join(self, timeout=2):
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _execute(self, run):
        definition = self.definitions.get(run["mission_id"])
        try:
            if definition is None:
                self._finish(run, "FAULT", "UNKNOWN_MISSION")
                return
            self._enter(run, "CHECKING", "checks")
            if self._stop.is_set():
                self._finish(run, "STOPPED", "STOP_REQUESTED")
                return
            if not self._checks(run, definition):
                return
            if self._stop.is_set():
                self._finish(run, "STOPPED", "STOP_REQUESTED")
                return
            self._enter(run, "RUNNING", "turn_on")
            if self._stop.is_set():
                self._finish(run, "STOPPED", "STOP_REQUESTED")
                return
            turned = self._command(run, definition, "turn_on", "normal")
            if not turned["accepted"]:
                self._finish(run, "FAULT", turned["reason"] or "HA_HTTP")
                return
            run["energized"] = True
            self._touch(run)
            if self._stop.is_set():
                self._cleanup(run, definition)
                return
            if not self._observe(run, definition, "on"):
                self._cleanup(run, definition, keep_reason=True)
                return
            if self._stop.is_set():
                self._cleanup(run, definition)
                return
            self._enter(run, "RUNNING", "wait")
            if not self._wait(definition.wait_s):
                self._cleanup(run, definition)
                return
            if self._stop.is_set():
                self._cleanup(run, definition)
                return
            self._enter(run, "RUNNING", "turn_off")
            if self._stop.is_set():
                self._cleanup(run, definition)
                return
            off = self._command(run, definition, "turn_off", "normal")
            if not off["accepted"]:
                run["reason"] = off["reason"] or "HA_HTTP"
                self._touch(run)
                self._cleanup(run, definition, keep_reason=True)
                return
            self._enter(run, "RUNNING", "verify")
            if not self._observe(run, definition, definition.final_state):
                self._finish(run, "FAULT", run.get("reason") or "ACTION_STATE_MISMATCH")
                return
            self._finish(run, "COMPLETE", None)
        except Exception:
            self._finish(run, "FAULT", "MISSION_FAULT")

    def _checks(self, run, definition):
        try:
            healthy, reason = self.health()
        except Exception:
            healthy, reason = False, "SYSTEM_UNKNOWN"
        if healthy is not True:
            return self._fail(run, "system_health", reason or "SYSTEM_UNHEALTHY")
        self._pass(run, "system_health")
        view_reason, entity = self._entity(definition.target)
        if view_reason and entity is None and view_reason != "UNKNOWN_ENTITY":
            return self._fail(run, "home_bridge", view_reason)
        self._pass(run, "home_bridge")
        if entity is None:
            return self._fail(run, "target_exists", "UNKNOWN_ENTITY")
        self._pass(run, "target_exists")
        if entity.get("entity_id") != definition.target:
            return self._fail(run, "target_approved", "TARGET_NOT_APPROVED")
        self._pass(run, "target_approved")
        if entity.get("available") is not True:
            return self._fail(run, "target_available", "TARGET_UNAVAILABLE")
        self._pass(run, "target_available")
        capabilities = entity.get("capabilities") if isinstance(entity.get("capabilities"), list) else []
        if entity.get("writable") is not True or "turn_on" not in capabilities or "turn_off" not in capabilities:
            reason = "CAPABILITY_UNAVAILABLE" if entity.get("writable") is True else "TARGET_NOT_WRITABLE"
            return self._fail(run, "target_writable", reason)
        self._pass(run, "target_writable")
        state = entity.get("state")
        if not isinstance(state, str) or state in {"", "unknown", "unavailable"}:
            return self._fail(run, "start_state", "START_STATE_UNKNOWN")
        self._pass(run, "start_state")
        return True

    def _observe(self, run, definition, want):
        deadline = self.clock() + definition.observe_timeout_s
        last_reason = "ACTION_STATE_MISMATCH"
        while True:
            view_reason, entity = self._entity(definition.target)
            state = entity.get("state") if isinstance(entity, dict) else None
            if view_reason is None and state == want:
                if want == definition.final_state:
                    run["energized"] = False
                self._pass(run, "observe_" + want)
                return True
            if view_reason and view_reason != "UNKNOWN_ENTITY":
                last_reason = view_reason
                break
            if view_reason == "UNKNOWN_ENTITY":
                last_reason = "UNKNOWN_ENTITY"
                break
            if self.clock() >= deadline:
                break
            self.sleeper(min(SLICE_S, max(0.0, deadline - self.clock())))
        run["reason"] = last_reason
        self._fail_check_only(run, "observe_" + want, last_reason)
        return False

    def _wait(self, seconds):
        deadline = self.clock() + seconds
        while self.clock() < deadline:
            if self._stop.is_set():
                return False
            self.sleeper(min(SLICE_S, max(0.0, deadline - self.clock())))
        return not self._stop.is_set()

    def _cleanup(self, run, definition, keep_reason=False):
        """One safe turn_off, then the bounded final-state observation. No retry."""
        prior = run.get("reason")
        self._enter(run, "RUNNING", "cleanup")
        record = self._command(run, definition, "turn_off", "cleanup")
        verified = self._observe(run, definition, definition.final_state)
        run["cleanup"] = {
            "accepted": record["accepted"],
            "reason": record["reason"],
            "verified": verified,
        }
        self._touch(run)
        if keep_reason:
            self._finish(run, "FAULT", prior or "ACTION_STATE_MISMATCH")
            return
        self._finish(run, "STOPPED", "STOP_REQUESTED" if verified else "CLEANUP_FAILED")

    def _command(self, run, definition, action, phase):
        if action not in {"turn_on", "turn_off"}:
            result = {"accepted": False, "reason": "MALFORMED_PARAMETERS"}
        else:
            try:
                result = self.home.act(definition.target, action)
            except Exception:
                result = {"accepted": False, "reason": "HA_UNAVAILABLE"}
        if not isinstance(result, dict):
            result = {"accepted": False, "reason": "HA_MALFORMED"}
        _reason, entity = self._entity(definition.target)
        observed = entity.get("state") if isinstance(entity, dict) else None
        record = {
            "action": action,
            "target": definition.target,
            "accepted": result.get("accepted") is True,
            "reason": result.get("reason"),
            "observed_state": observed,
            "phase": phase,
        }
        run["actions"].append(record)
        self._touch(run)
        return record

    def _entity(self, target):
        if self.home is None:
            return "HA_UNCONFIGURED", None
        try:
            view = self.home.entity_view()
        except Exception:
            return "HA_UNAVAILABLE", None
        if not isinstance(view, dict) or view.get("available") is not True:
            reason = view.get("reason") if isinstance(view, dict) else None
            return reason or "HA_UNAVAILABLE", None
        for entity in view.get("entities") or []:
            if isinstance(entity, dict) and entity.get("entity_id") == target:
                return None, entity
        return "UNKNOWN_ENTITY", None

    def _enter(self, run, state, step):
        run["state"] = state
        run["step"] = step
        run["updated_at"] = _stamp()
        self._touch(run)
        if self.before_step is not None:
            self.before_step(run)

    def _pass(self, run, name):
        run["checks"].append({"name": name, "outcome": "pass", "reason": None})
        self._touch(run)

    def _fail(self, run, name, reason):
        run["checks"].append({"name": name, "outcome": "fail", "reason": reason})
        self._finish(run, "FAULT", reason)
        return False

    def _fail_check_only(self, run, name, reason):
        run["checks"].append({"name": name, "outcome": "fail", "reason": reason})
        self._touch(run)

    def _touch(self, run):
        run["updated_at"] = _stamp()
        with self._lock:
            self.store.touch()

    def _finish(self, run, state, reason):
        with self._lock:
            if run.get("state") in TERMINAL:
                return
            now = _stamp()
            run["state"] = state
            run["reason"] = reason
            run["updated_at"] = now
            run["completed_at"] = now
            if self._active is run:
                self._active = None
            self._stop.clear()
            self.store.touch()


def open_engine(definitions_path, home, health, store_path, **kwargs):
    """Open the engine. A bad mission file disables starts and does not raise."""
    store = RunStore(store_path)
    try:
        definitions = load_definitions(definitions_path)
    except ValueError as exc:
        return MissionEngine({}, store, home, health, available=False, reason=str(exc), **kwargs)
    except Exception:
        return MissionEngine({}, store, home, health, available=False, reason="MISSION_DEFINITION_INVALID", **kwargs)
    return MissionEngine(definitions, store, home, health, **kwargs)
