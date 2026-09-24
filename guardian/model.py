"""Pure evidence reduction. Wire statuses follow snapshot schema v1 (lowercase)."""
from copy import deepcopy
from datetime import datetime, timezone


def utcnow():
    return datetime.now(timezone.utc).timestamp()


def stamp(now):
    return datetime.fromtimestamp(now, timezone.utc).isoformat().replace('+00:00', 'Z')


def age(ts, now):
    try:
        value = now - datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp()
        return value if value >= -5 else None
    except (ValueError, TypeError, AttributeError):
        return None


def fact(status, required, now, trace, cls=None, **data):
    return dict(status=status, required=required, observed_at=stamp(now),
                observation_age_s=0, trace_id=trace, **{'class': cls}, **data)


def fresh(item, now, max_age=90):
    result = deepcopy(item)
    elapsed = age(result.get('observed_at'), now)
    result['observation_age_s'] = elapsed
    if elapsed is None or elapsed > max_age:
        result.update(status='unknown', **{'class': 'EVIDENCE_STALE'})
    return result


def reduce_layers(layers):
    """Reduce parent health without letting optional hard failures block it.

    Required RED/UNKNOWN/YELLOW evidence controls parent health. Optional RED or
    UNKNOWN layers stay visible on their own chips/cards but do not degrade the
    parent. Optional YELLOW remains a soft parent degradation for catalog drift
    and unresolved conflicts.
    """
    required = [x for x in layers.values() if x['required']]
    for status in ('red', 'unknown', 'yellow'):
        if any(x['status'] == status for x in required):
            return status
    if any(x['status'] == 'yellow' for x in layers.values() if not x['required']):
        return 'yellow'
    return 'green'


def reduce_system(objects, guardian, now, hard_stale=120):
    elapsed = age(guardian.get('heartbeat_at'), now)
    if elapsed is None or elapsed > hard_stale:
        return 'unknown', 'GUARDIAN_HEARTBEAT_STALE'
    if guardian.get('last_run_result') == 'crash':
        return 'unknown', 'PROBE_CRASH'
    required = [x for x in objects if x['required']]
    for status in ('red', 'unknown', 'yellow'):
        if any(x['status'] == status for x in required):
            return status, None
    return 'green', None


def reduce_object(identifier, required, layers, now, trace, **extra):
    status = reduce_layers(layers)
    cls = next((x.get('class') for x in layers.values()
                if x['status'] == status and x.get('class')), None)
    if status == 'yellow' and cls is None:
        cls = next((x.get('class') for x in layers.values()
                    if x['status'] == 'yellow' and x.get('class')), None)
    return fact(status, required, now, trace, cls, id=identifier, layers=layers, **extra)