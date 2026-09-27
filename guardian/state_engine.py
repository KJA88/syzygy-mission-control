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
