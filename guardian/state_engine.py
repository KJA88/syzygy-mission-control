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
    if confidence is not None and not 0.0 <= float(confidence) <= 1.0:
        raise ValueError("confidence must be between 0 and 1")
    if knowledge == "unknown":
        value = None
        confidence = None
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
    if item.get("status") == "unknown" or item.get("observation_age_s") is None:
        return "unknown"
    if item.get("class") in ("EVIDENCE_STALE", "SNAPSHOT_STALE", "OBSERVATION_STALE"):
        return "stale"
    return "fresh"


def _health(item, source, trace_id):
    status = item.get("status")
    if status not in ("green", "yellow", "red"):
        return unknown(source, trace_id, item.get("class") or "EVIDENCE_MISSING")
    return assertion(status, "derived", _freshness(item), source,
                     item.get("observed_at"), item.get("trace_id") or trace_id,
                     confidence=1.0, reason=item.get("class"))


def _entity(identifier, kind, attributes):
    return {"id": identifier, "kind": kind, "attributes": attributes}


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
                 trace_id=guardian.get("trace_id")),
            "guardian/reducer", trace),
        "current_mission": unknown("mission-engine", trace, "NOT_IMPLEMENTED"),
        "active_operator": unknown("operator-registry", trace, "NOT_IMPLEMENTED"),
    }))

    for node in snapshot.get("nodes") or []:
        attrs = {"health": _health(node, "guardian/node/" + node.get("id", "unknown"), trace)}
        for name, value in (node.get("metrics") or {}).items():
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
        attrs = {
            "health": _health(service, "guardian/service/" + service_id, trace),
            "host": assertion(service.get("host"), "configured", "fresh",
                              "guardian/config", service.get("observed_at"),
                              service.get("trace_id") or trace, confidence=1.0),
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

    return {
        "schema_version": 1,
        "generated_at": snapshot.get("generated_at"),
        "trace_id": trace,
        "read_only": True,
        "entities": entities,
    }
