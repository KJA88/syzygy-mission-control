"""Read-only Phase 2 state view derived from Guardian evidence.

This module does not probe devices or issue commands.  It translates the
existing infrastructure snapshot into a coherent entity/attribute model while
preserving provenance, timestamps, freshness, and explicit unknowns.
"""
from copy import deepcopy


KNOWLEDGE = frozenset({"observed", "derived", "configured", "remembered", "requested", "verified", "unknown"})
FRESHNESS = frozenset({"fresh", "stale", "expired", "unknown"})


def assertion(value, knowledge, freshness, source, observed_at, trace_id,
              confidence=None, reason=None):
    if knowledge not in KNOWLEDGE:
        raise ValueError("invalid knowledge type: " + str(knowledge))
    if freshness not in FRESHNESS:
        raise ValueError("invalid freshness: " + str(freshness))
    if knowledge == "unknown":
        value = None
        confidence = None
    elif value is None:
        raise ValueError("null values must use unknown knowledge")
    elif confidence is not None:
        confidence = float(confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
    return {
        "value": deepcopy(value),
        "knowledge": knowledge,
        "freshness": freshness,
        "source": source,
        "observed_at": observed_at,
        "trace_id": trace_id,
        "confidence": confidence,
        "reason": reason,
    }


def unknown(source, trace_id, reason="EVIDENCE_MISSING"):
    return assertion(None, "unknown", "unknown", source, None, trace_id,
                     reason=reason)


def _freshness(item):
    classification = item.get("class")
    if classification == "AGENT_LATE" or str(classification).endswith("_STALE"):
        return "stale"
    if str(classification).endswith("_EXPIRED"):
        return "expired"
    if item.get("status") == "unknown" or item.get("observation_age_s") is None:
        return "unknown"
    return "fresh"


def _health(item, source, trace_id):
    status = item.get("status")
    if status not in ("green", "yellow", "red"):
        return assertion(
            None, "unknown", _freshness(item), source, item.get("observed_at"),
            item.get("trace_id") or trace_id, reason=item.get("class") or "EVIDENCE_MISSING")
    return assertion(status, "derived", _freshness(item), source,
                     item.get("observed_at"), item.get("trace_id") or trace_id,
                     confidence=1.0, reason=item.get("class"))


def _entity(identifier, kind, attributes):
    return {"id": identifier, "kind": kind, "attributes": attributes}


def _seen(value, missing_reason, source, observed_at, freshness, trace):
    if value is None or value == "":
        return unknown(source, trace, missing_reason)
    return assertion(
        value, "observed", freshness, source, observed_at, trace, confidence=1.0)


def _roarm_attributes(roarm, trace):
    """Communication facts already collected by the RoArm probe.

    T105 evidence uses the probe timestamp. Transport-status evidence uses
    the status file's own updated_at and is stale on that clock alone.
    """
    source = "guardian/roarm"
    t105_at = roarm.get("t105_observed_at") or roarm.get("observed_at")
    t105_freshness = "fresh" if roarm.get("t105_fresh") is True and t105_at else "unknown"
    updated_at = roarm.get("transport_status_updated_at")
    if roarm.get("transport_status_fresh") is True and updated_at:
        transport_freshness = "fresh"
    elif updated_at:
        transport_freshness = "stale"
    else:
        transport_freshness = "unknown"
    failure = roarm.get("failure_reason")
    if not failure and roarm.get("class") and roarm.get("error"):
        failure = str(roarm.get("error"))
    route = roarm.get("route") if isinstance(roarm.get("route"), dict) else {}
    return {
        "reachability": _seen(
            roarm.get("reachability"), "EVIDENCE_MISSING", source,
            t105_at, t105_freshness, trace),
        "route": _seen(
            route.get("status"), "ROUTE_UNAVAILABLE", source,
            t105_at, t105_freshness, trace),
        "t105_fresh": _seen(
            roarm.get("t105_fresh"), "T105_UNAVAILABLE", source,
            t105_at, t105_freshness, trace),
        "transport_state": _seen(
            roarm.get("transport_state"), "TRANSPORT_STATUS_UNAVAILABLE", source,
            updated_at, transport_freshness, trace),
        "udp_target": assertion(
            roarm.get("udp_target") or "192.168.4.1:4210",
            "configured", "fresh", "guardian/config", None, trace, confidence=1.0),
        "stream_id": _seen(
            roarm.get("stream_id"), "STREAM_ID_UNAVAILABLE", source,
            updated_at, transport_freshness, trace),
        "last_sequence": _seen(
            roarm.get("last_sequence"), "SEQUENCE_UNAVAILABLE", source,
            updated_at, transport_freshness, trace),
        "last_completion": _seen(
            roarm.get("last_completion"), "COMPLETION_UNAVAILABLE", source,
            updated_at, transport_freshness, trace),
        "udp_late": _seen(
            roarm.get("last_late"), "LATE_SEND_UNAVAILABLE", source,
            updated_at, transport_freshness, trace),
        "udp_failed": _seen(
            roarm.get("last_failed"), "FAILED_SEND_UNAVAILABLE", source,
            updated_at, transport_freshness, trace),
        "watchdog_release": _seen(
            roarm.get("last_watchdog"), "WATCHDOG_RELEASE_UNAVAILABLE", source,
            updated_at, transport_freshness, trace),
        "failure_reason": (
            _seen(failure, "NO_ACTIVE_FAULT", source, t105_at, t105_freshness, trace)
            if failure else unknown(source, trace, "NO_ACTIVE_FAULT")
        ),
        "serial_fallback": assertion(
            False, "configured", "fresh", "guardian/config", None, trace, confidence=1.0),
    }


def build_state(snapshot):
    """Build schema-v1 operational state without changing source evidence."""
    trace = snapshot.get("trace_id")
    entities = []
    system = snapshot.get("system") or {}
    guardian = snapshot.get("guardian") or {}
    entities.append(_entity("system/syzygy", "system", {
        "health": _health(
            dict(system, observed_at=guardian.get("observed_at"),
                 observation_age_s=guardian.get("observation_age_s"),
                 trace_id=guardian.get("trace_id"),
                 **{"class": system.get("reason")}),
            "guardian/reducer", trace),
        "current_mission": unknown("mission-engine", trace, "NOT_IMPLEMENTED"),
        "active_operator": unknown("operator-registry", trace, "NOT_IMPLEMENTED"),
    }))

    for node in snapshot.get("nodes") or []:
        attrs = {"health": _health(node, "guardian/node/" + node.get("id", "unknown"), trace)}
        metrics = node.get("metrics")
        if not isinstance(metrics, dict):
            metrics = {}
        for name, value in metrics.items():
            if value is None:
                attrs["metric/" + name] = unknown(
                    "agent/" + node.get("id", "unknown"),
                    node.get("trace_id") or trace, "METRIC_UNAVAILABLE")
            else:
                attrs["metric/" + name] = assertion(
                    value, "observed", _freshness(node),
                    "agent/" + node.get("id", "unknown"), node.get("observed_at"),
                    node.get("trace_id") or trace, confidence=1.0)
        entities.append(_entity("node/" + node.get("id", "unknown"), "node", attrs))

    for service in snapshot.get("services") or []:
        service_id = service.get("id", "unknown")
        host = service.get("host")
        attrs = {
            "health": _health(service, "guardian/service/" + service_id, trace),
            "host": (
                assertion(host, "configured", "fresh", "guardian/config", None,
                          service.get("trace_id") or trace, confidence=1.0)
                if host is not None
                else unknown("guardian/config", service.get("trace_id") or trace,
                             "CONFIG_MISSING")
            ),
        }
        if service_id == "tv-mcp":
            attrs.update({
                "power": unknown("tv-state-adapter", trace, "ADAPTER_NOT_IMPLEMENTED"),
                "requested_input": unknown("tv-state-adapter", trace, "ADAPTER_NOT_IMPLEMENTED"),
                "verified_input": unknown("tv-state-adapter", trace, "ADAPTER_NOT_IMPLEMENTED"),
            })
        entities.append(_entity("service/" + service_id, "service", attrs))
        for camera_id, camera in (service.get("cameras") or {}).items():
            entities.append(_entity("camera/" + camera_id, "camera", {
                "health": _health(camera, "guardian/camera/" + camera_id, trace),
                "online": assertion(
                    True if camera.get("status") == "green" else False if camera.get("status") == "red" else None,
                    "derived" if camera.get("status") in ("green", "red") else "unknown",
                    _freshness(camera), "guardian/camera/" + camera_id,
                    camera.get("observed_at"), camera.get("trace_id") or trace,
                    confidence=1.0 if camera.get("status") in ("green", "red") else None,
                    reason=camera.get("class")),
            }))

    roarm = snapshot.get("roarm")
    if isinstance(roarm, dict):
        entities.append(_entity("device/roarm", "device", _roarm_attributes(roarm, trace)))

    return {
        "schema_version": 1,
        "generated_at": snapshot.get("generated_at"),
        "trace_id": trace,
        "read_only": True,
        "entities": entities,
    }


# Authoritative operational state. Distinct from Guardian observations above.
# Phase 2 records commanded state only. It does not enforce arm motion.

OPERATIONAL_STATES = ("IDLE", "PREPARING", "MOVING", "COMPLETE", "FAULT", "STOPPED")
ACTIVE_MOTION_STATES = frozenset({"PREPARING", "MOVING"})
KNOWN_GOOD_STATES = frozenset({"IDLE", "COMPLETE"})
LEGAL_TRANSITIONS = {
    "IDLE": frozenset({"PREPARING", "FAULT", "STOPPED"}),
    "PREPARING": frozenset({"IDLE", "MOVING", "COMPLETE", "FAULT", "STOPPED"}),
    "MOVING": frozenset({"COMPLETE", "FAULT", "STOPPED"}),
    "COMPLETE": frozenset({"IDLE", "PREPARING", "FAULT", "STOPPED"}),
    "FAULT": frozenset({"IDLE", "STOPPED"}),
    "STOPPED": frozenset({"IDLE", "FAULT"}),
}
_UNSET = object()


def _text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _new_operational_state(now, trace_id, reason):
    from .model import stamp
    return {
        "schema_version": 1,
        "role": "authoritative",
        "state": "IDLE",
        "previous_state": None,
        "transition_at": stamp(now),
        "transition_reason": reason,
        "operator": None,
        "skill": None,
        "mission": None,
        "motion_permitted": False,
        "motion_authority": None,
        "fault_class": None,
        "fault_reason": None,
        "last_known_good": "IDLE",
        "last_completed_action": None,
        "trace_id": trace_id,
        "freshness": "fresh",
        "last_rejection": None,
    }


def _valid_operational_record(raw):
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        return False
    if raw.get("role") != "authoritative":
        return False
    if raw.get("state") not in OPERATIONAL_STATES:
        return False
    if not isinstance(raw.get("transition_reason"), str) or not raw.get("trace_id"):
        return False
    good = raw.get("last_known_good")
    return good in KNOWN_GOOD_STATES


def recover_operational_state(raw, now, trace_id):
    """Return a safe authoritative record. Do not claim unproven motion."""
    if raw is None:
        return _new_operational_state(now, trace_id, "INITIAL")
    if not _valid_operational_record(raw):
        return _new_operational_state(now, trace_id, "RECOVERY_INVALID")
    if raw["state"] == "FAULT" and not _text(raw.get("fault_class")):
        return _new_operational_state(now, trace_id, "RECOVERY_INVALID")
    if raw["state"] in ACTIVE_MOTION_STATES:
        recovered = _new_operational_state(now, trace_id, "RECOVERY_UNPROVEN_MOTION")
        recovered["state"] = "STOPPED"
        recovered["previous_state"] = raw["state"]
        recovered["operator"] = _text(raw.get("operator"))
        recovered["last_known_good"] = raw["last_known_good"]
        recovered["last_completed_action"] = _text(raw.get("last_completed_action"))
        recovered["fault_class"] = _text(raw.get("fault_class"))
        recovered["fault_reason"] = _text(raw.get("fault_reason"))
        return recovered
    kept = _new_operational_state(now, trace_id, raw["transition_reason"])
    kept.update(
        state=raw["state"],
        previous_state=raw.get("previous_state") if raw.get("previous_state") in OPERATIONAL_STATES else None,
        transition_at=raw.get("transition_at") or kept["transition_at"],
        operator=_text(raw.get("operator")),
        mission=_text(raw.get("mission")) if raw["state"] == "COMPLETE" else None,
        fault_class=_text(raw.get("fault_class")) if raw["state"] in ("FAULT", "STOPPED") else None,
        fault_reason=_text(raw.get("fault_reason")) if raw["state"] in ("FAULT", "STOPPED") else None,
        last_known_good=raw["last_known_good"],
        last_completed_action=_text(raw.get("last_completed_action")),
        trace_id=str(raw["trace_id"]),
        last_rejection=raw.get("last_rejection") if isinstance(raw.get("last_rejection"), dict) else None,
    )
    if raw["state"] == "COMPLETE":
        kept["last_known_good"] = "COMPLETE"
    elif raw["state"] == "IDLE":
        kept["last_known_good"] = "IDLE"
    kept["skill"] = None
    kept["motion_authority"] = None
    kept["motion_permitted"] = False
    kept["freshness"] = "fresh"
    return kept


def _reject(record, target, reason, now, trace_id):
    from .model import stamp
    from copy import deepcopy
    rejected = deepcopy(record)
    rejected["last_rejection"] = {
        "at": stamp(now),
        "from_state": record.get("state"),
        "to_state": target,
        "reason": reason,
        "trace_id": trace_id,
    }
    return rejected


def apply_transition(record, target, *, now, trace_id, reason,
                     operator=_UNSET, skill=_UNSET, mission=_UNSET,
                     motion_authority=_UNSET, fault_class=_UNSET, fault_reason=_UNSET):
    """Accept one legal transition or record a rejection without changing state."""
    from .model import stamp
    from copy import deepcopy
    current = record.get("state")
    requested = str(target)
    if requested not in LEGAL_TRANSITIONS.get(current, frozenset()):
        return _reject(record, requested, "ILLEGAL_TRANSITION", now, trace_id), False
    if not _text(reason):
        return _reject(record, requested, "TRANSITION_REASON_REQUIRED", now, trace_id), False
    if requested == "FAULT" and not _text(fault_class if fault_class is not _UNSET else None):
        return _reject(record, requested, "FAULT_DETAIL_REQUIRED", now, trace_id), False
    prospective_authority = (
        record.get("motion_authority") if motion_authority is _UNSET else _text(motion_authority))
    if requested == "MOVING" and not prospective_authority:
        return _reject(record, requested, "AUTHORITY_REQUIRED", now, trace_id), False

    nxt = deepcopy(record)
    nxt["previous_state"] = current
    nxt["state"] = requested
    nxt["transition_at"] = stamp(now)
    nxt["transition_reason"] = _text(reason)
    nxt["trace_id"] = str(trace_id)
    nxt["freshness"] = "fresh"
    nxt.pop("recorded_state", None)
    nxt.pop("display_reason", None)

    def assign(field, supplied):
        if supplied is not _UNSET:
            nxt[field] = _text(supplied)

    assign("operator", operator)
    assign("skill", skill)
    assign("mission", mission)
    assign("motion_authority", motion_authority)
    assign("fault_class", fault_class)
    assign("fault_reason", fault_reason)

    if requested == "MOVING":
        nxt["motion_permitted"] = True
        nxt["fault_class"] = None
        nxt["fault_reason"] = None
    elif requested == "PREPARING":
        nxt["motion_permitted"] = bool(nxt.get("motion_authority"))
        nxt["fault_class"] = None
        nxt["fault_reason"] = None
    elif requested == "COMPLETE":
        nxt["last_completed_action"] = nxt.get("skill")
        nxt["skill"] = None
        nxt["motion_authority"] = None
        nxt["motion_permitted"] = False
        nxt["fault_class"] = None
        nxt["fault_reason"] = None
        nxt["last_known_good"] = "COMPLETE"
    elif requested == "IDLE":
        nxt["skill"] = None
        nxt["mission"] = None
        nxt["motion_authority"] = None
        nxt["motion_permitted"] = False
        nxt["fault_class"] = None
        nxt["fault_reason"] = None
        nxt["last_known_good"] = "IDLE"
    elif requested == "FAULT":
        nxt["motion_permitted"] = False
        nxt["motion_authority"] = None
        nxt["skill"] = None
    elif requested == "STOPPED":
        nxt["motion_permitted"] = False
        nxt["motion_authority"] = None
        nxt["skill"] = None
        if current != "FAULT":
            nxt["fault_class"] = None
            nxt["fault_reason"] = None
    return nxt, True


def mark_operational_unproven(state_engine):
    """A stale snapshot must not present PREPARING or MOVING as current."""
    if not isinstance(state_engine, dict):
        return
    operational = state_engine.get("operational")
    if not isinstance(operational, dict):
        return
    operational["freshness"] = "stale"
    if operational.get("state") in ACTIVE_MOTION_STATES:
        operational["recorded_state"] = operational.get("state")
        operational["state"] = None
        operational["motion_permitted"] = False
        operational["display_reason"] = "UNPROVEN_ACTIVE_STATE"


def unavailable_operational():
    """Published when the control owner has not written a record. This does not create one."""
    return {
        "schema_version": 1,
        "role": "authoritative",
        "state": None,
        "freshness": "unknown",
        "motion_permitted": False,
        "operator": None,
        "skill": None,
        "mission": None,
        "motion_authority": None,
        "fault_class": None,
        "fault_reason": None,
        "last_completed_action": None,
        "transition_reason": None,
        "previous_state": None,
        "transition_at": None,
        "trace_id": None,
        "last_known_good": None,
        "last_rejection": None,
        "display_reason": "OPERATIONAL_STATE_UNAVAILABLE",
    }


def load_published_operational(path):
    """Read the control owner's record. Guardian must not write this file."""
    import json
    from pathlib import Path
    file = Path(path)
    if not file.is_file():
        return unavailable_operational()
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return unavailable_operational()
    if not isinstance(raw, dict) or not _valid_operational_record(raw):
        return unavailable_operational()
    if raw.get("state") == "FAULT" and not _text(raw.get("fault_class")):
        return unavailable_operational()
    record = dict(raw)
    record["freshness"] = "fresh"
    return record


def attach_operational_state(snapshot, record):
    """Copy authoritative state onto the snapshot. Evidence entities stay unchanged."""
    from copy import deepcopy
    engine = snapshot.get("state_engine")
    if not isinstance(engine, dict):
        engine = {}
        snapshot["state_engine"] = engine
    engine["operational"] = deepcopy(record)
    return snapshot


class OperationalStateStore:
    """Single writer for the authoritative operational record."""

    def __init__(self, path):
        from pathlib import Path
        self.path = Path(path)
        self.record = None

    def recover(self, now, trace_id):
        import json
        raw = None
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {"schema_version": 0}
        self.record = recover_operational_state(raw, now, trace_id)
        self.save()
        return self.view()

    def transition(self, target, **kwargs):
        if self.record is None:
            raise RuntimeError("operational state has not been recovered")
        self.record, accepted = apply_transition(self.record, target, **kwargs)
        self.save()
        return self.view(), accepted

    def save(self):
        from .storage import atomic_json
        if self.record is None:
            raise RuntimeError("operational state has not been recovered")
        atomic_json(self.path, self.record)

    def view(self):
        from copy import deepcopy
        return deepcopy(self.record)
