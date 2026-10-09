"""Capability checks. Restricted tools are visible and not executable."""
from __future__ import annotations

import json
import logging

from registry.authz import Authorization
from registry.catalog import Catalog
from registry.health import HealthBridge, HealthError

LOGGER = logging.getLogger("syzygy.registry")

PROXY = {
    "health.read": "read",
    "health.write": "write",
    "health.audit": "audit",
}


def invoke(catalog: Catalog, health: HealthBridge | None, name: str, arguments: dict,
           authorization: Authorization, write_log=None) -> dict:
    profile = authorization.profile
    if name not in catalog.capabilities:
        return {"accepted": False, "reason": "UNKNOWN_CAPABILITY"}
    try:
        allowed = catalog.permissions(profile)
    except KeyError:
        return {"accepted": False, "reason": "UNKNOWN_PROFILE", "profile": profile}
    cap = catalog.capabilities[name]
    if not _arguments_match(cap["input_schema"], arguments):
        return {"accepted": False, "reason": "MALFORMED_PARAMETERS", "capability": name}
    if cap["execution"] == "restricted" or cap["permission"] == "restricted":
        return {
            "accepted": False,
            "reason": cap.get("reject_reason") or "RESTRICTED",
            "capability": name,
            "owner": cap["owner"],
            "safety": list(cap["safety"]),
        }
    if cap["permission"] not in allowed:
        return {
            "accepted": False,
            "reason": "PERMISSION_DENIED",
            "capability": name,
            "permission": cap["permission"],
        }
    if cap["execution"] == "registry":
        return {"accepted": True, "reason": None, "capability": name, "result": _registry(catalog, name, arguments, profile)}
    if cap["execution"] == "declared":
        return {"accepted": True, "reason": None, "capability": name, "result": _declared(catalog, cap)}
    if cap["execution"] == "proxy":
        payload = _proxy(health, name, arguments)
        if name == "health.write":
            _log_health_write(authorization, payload, write_log)
        return payload
    return {"accepted": False, "reason": "UNKNOWN_CAPABILITY", "capability": name}


def _log_health_write(authorization: Authorization, payload: dict, write_log) -> None:
    record = {
        "event": "health_write",
        "actor": authorization.actor,
        "identity": authorization.identity,
        "profile": authorization.profile,
        "capability": "health.write",
        "accepted": payload.get("accepted") is True,
    }
    LOGGER.info(json.dumps(record, sort_keys=True))
    if write_log is not None:
        write_log.append(record)


def _registry(catalog: Catalog, name: str, arguments: dict, profile: str):
    if name == "syzygy.systems":
        return {"systems": catalog.systems()}
    if name == "syzygy.tools":
        return {"profile": profile, "tools": catalog.tools(profile)}
    if name == "syzygy.capability":
        target = arguments["name"]
        if target not in catalog.capabilities:
            return {"accepted": False, "reason": "UNKNOWN_CAPABILITY", "name": target}
        record = catalog.capability(target)
        record["owner_record"] = catalog.owner_of(target)
        return record
    if name == "syzygy.safety":
        target = arguments["name"]
        try:
            return catalog.safety_of(target)
        except KeyError:
            return {"accepted": False, "reason": "UNKNOWN_CAPABILITY", "name": target}
    if name == "syzygy.state":
        return catalog.state()
    return {"accepted": False, "reason": "UNKNOWN_CAPABILITY"}


def _declared(catalog: Catalog, cap: dict) -> dict:
    system = catalog.subsystems[cap["subsystem"]]
    result = {
        "subsystem": system["id"],
        "name": system["name"],
        "owner": cap["owner"],
        "endpoint": cap["endpoint"],
        "docs": cap["docs"],
        "availability": cap["availability"],
        "safety": list(cap["safety"]),
        "probed": False,
    }
    if system.get("public_endpoint"):
        result["public_endpoint"] = system["public_endpoint"]
    if system.get("services"):
        result["services"] = list(system["services"])
    if system.get("host"):
        result["host"] = system["host"]
    if system.get("expected_tools") is not None:
        result["expected_tools"] = system["expected_tools"]
    if cap["name"] == "camera.list":
        result["cameras"] = list(system.get("cameras") or [])
    if "declared_result" in cap:
        result.update(cap["declared_result"])
    return result


def _proxy(health: HealthBridge | None, name: str, arguments: dict) -> dict:
    if health is None or name not in PROXY:
        return {"accepted": False, "reason": "UNSUPPORTED_PROXY", "capability": name}
    try:
        payload = getattr(health, PROXY[name])(arguments)
    except HealthError as exc:
        body = {
            "accepted": False,
            "reason": exc.code,
            "capability": name,
            "detail": exc.detail,
        }
        if exc.row_id:
            body["row_id"] = exc.row_id
        return body
    return {"accepted": True, "reason": None, "capability": name, "result": payload}


def _arguments_match(schema: dict, arguments: dict) -> bool:
    if not isinstance(arguments, dict):
        return False
    properties = schema.get("properties") or {}
    if schema.get("additionalProperties") is False and set(arguments).difference(properties):
        return False
    required = schema.get("required") or []
    return all(name in arguments for name in required)
