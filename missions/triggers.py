"""Normalized trigger envelopes. Perception envelopes do not start missions."""
from __future__ import annotations

from datetime import datetime, timezone


def _stamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def operator_trigger(operator=None):
    metadata = {}
    name = _text(operator)
    if name:
        metadata["operator"] = name
    return {
        "source": "operator",
        "event_type": "start",
        "timestamp": _stamp(),
        "camera": None,
        "entity_id": None,
        "confidence": None,
        "metadata": metadata,
    }


def perception_trigger(event):
    """Copy the fields a future mission may check. This does not choose an action."""
    event = event if isinstance(event, dict) else {}
    confidence = event.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        confidence = None
    label = _text(event.get("event_type")) or _text(event.get("class"))
    return {
        "source": "perception",
        "event_type": label or "detection",
        "timestamp": _text(event.get("timestamp")) or _stamp(),
        "camera": _text(event.get("camera")),
        "entity_id": None,
        "confidence": confidence,
        "metadata": {},
    }


def validate_start_trigger(trigger):
    """Operator starts are allowed. Perception starts are defined and refused."""
    if trigger is None:
        return operator_trigger(), None
    if not isinstance(trigger, dict):
        return None, "MALFORMED_PARAMETERS"
    source = trigger.get("source")
    if source == "perception":
        return None, "TRIGGER_NOT_ENABLED"
    if source != "operator":
        return None, "MALFORMED_PARAMETERS"
    event_type = trigger.get("event_type")
    if event_type is not None and not isinstance(event_type, str):
        return None, "MALFORMED_PARAMETERS"
    return {
        "source": "operator",
        "event_type": event_type or "start",
        "timestamp": trigger.get("timestamp") if isinstance(trigger.get("timestamp"), str) else _stamp(),
        "camera": trigger.get("camera") if isinstance(trigger.get("camera"), str) else None,
        "entity_id": None,
        "confidence": None,
        "metadata": trigger.get("metadata") if isinstance(trigger.get("metadata"), dict) else {},
    }, None
