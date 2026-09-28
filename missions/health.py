"""Decide whether required SYZYGY health allows a physical mission to start."""

GROUPS = ("nodes", "services", "paths", "auth")


def required_health(snapshot):
    """Return (ok, reason) from a Guardian snapshot.

    Optional yellow/red objects do not block a mission. A red system rollup
    or any required object that is not green does.
    """
    if not isinstance(snapshot, dict):
        return False, "SYSTEM_UNKNOWN"
    system = snapshot.get("system") if isinstance(snapshot.get("system"), dict) else {}
    status = system.get("status")
    if status == "red":
        return False, "SYSTEM_UNHEALTHY"
    if status not in {"green", "yellow"}:
        return False, "SYSTEM_UNKNOWN"
    for group in GROUPS:
        items = snapshot.get(group)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict) or item.get("required") is not True:
                continue
            if item.get("status") != "green":
                return False, "SYSTEM_UNHEALTHY"
    return True, None
