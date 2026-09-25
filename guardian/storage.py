"""Single-writer durable state output."""
import json
import os
import tempfile
from pathlib import Path


def _mark_state_engine_stale(snapshot):
    state = snapshot.get('state_engine')
    if not isinstance(state, dict):
        return
    for entity in state.get('entities', []):
        if not isinstance(entity, dict):
            continue
        attributes = entity.get('attributes')
        if not isinstance(attributes, dict):
            continue
        for assertion in attributes.values():
            if (isinstance(assertion, dict)
                    and assertion.get('knowledge') in (
                        'observed', 'derived', 'verified', 'requested', 'remembered')):
                assertion['freshness'] = 'stale'
    system = next(
        (entity for entity in state.get('entities', [])
         if isinstance(entity, dict) and entity.get('id') == 'system/syzygy'),
        None)
    if isinstance(system, dict):
        health = (system.get('attributes') or {}).get('health')
        if isinstance(health, dict):
            health.update(value=None, knowledge='unknown', freshness='stale',
                          confidence=None, reason='SNAPSHOT_STALE')


def read_snapshot(path, now, hard_stale=120):
    from .model import reduce_system
    snapshot = json.loads(Path(path).read_text(encoding='utf-8'))
    status, reason = reduce_system([], snapshot['guardian'], now, hard_stale)
    if status == 'unknown':
        snapshot['system'].update(status=status, reason=reason)
        snapshot['guardian'].update(status='unknown', **{'class': 'SNAPSHOT_STALE' if reason == 'GUARDIAN_HEARTBEAT_STALE' else reason})
        if reason == 'GUARDIAN_HEARTBEAT_STALE':
            _mark_state_engine_stale(snapshot)
    return snapshot


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, allow_nan=False, separators=(',', ':'))
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name == 'posix':
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def append_events(path, events):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as stream:
        for event in events:
            stream.write(json.dumps(event, allow_nan=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def changes(previous, current):
    def objects(snapshot):
        result = {}
        for group in ('nodes', 'services', 'paths', 'auth'):
            for obj in snapshot.get(group, []):
                result[group + '/' + obj['id']] = obj
                for name, layer in obj.get('layers', {}).items():
                    result[group + '/' + obj['id'] + '/' + name] = layer
        result['system'] = snapshot.get('system', {})
        return result
    old = objects(previous)
    events = []
    for key, obj in objects(current).items():
        before = old.get(key, {})
        if (before.get('status'), before.get('class')) == (obj.get('status'), obj.get('class')):
            continue
        recovered = before.get('status') in ('red', 'yellow', 'unknown') and obj.get('status') == 'green'
        events.append(dict(schema_version=1, ts=current['generated_at'], trace_id=current['trace_id'],
                           host=obj.get('host'), component=key, event_type='status_change',
                           severity='info' if obj.get('status') == 'green' else 'warning',
                           operation='health_probe', operator='guardian', duration_ms=None,
                           result=obj.get('status'), **{'class': 'RECOVERED' if recovered else obj.get('class')},
                           message=key + ' status changed', data={'before': before.get('status'), 'after': obj.get('status')}))
    return events
