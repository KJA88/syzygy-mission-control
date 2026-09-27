"""Read-only RoArm communication probe.

The only firmware command issued by this module is T=105.
Route, UDP target, and trajectory status are observations or static
configuration. USB serial is never selected as a runtime path.
"""
import json
import subprocess
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from .model import age, stamp, utcnow


ARM_HOST = '192.168.4.1'
PI_SOURCE = '192.168.4.2'
ROUTE_DEVICE = 'wlan0'
UDP_PORT = 4210
UDP_TARGET = '192.168.4.1:4210'
TRANSPORT_STATUS_FRESH_S = 90
DEFAULT_RUNTIME_DIR = Path('/home/KA_PI/syzygy-runtime/roarm')
STATUS_PATH = DEFAULT_RUNTIME_DIR / 'transport-status.json'
_TRANSPORT_STATES = {'idle', 'http', 'udp'}
_ACTIVE_FAILURES = {'PATTERN_UDP_LATE', 'PATTERN_UDP_FAILED'}


def unknown_roarm(endpoint, error):
    return {
        'status': 'unknown',
        'class': 'ROARM_UNREACHABLE',
        'connected': False,
        'fresh': False,
        'transport': 'http',
        'endpoint': endpoint,
        'error': str(error),
        'observed_at': stamp(utcnow()),
    }


def read_arm_route():
    """Ask the local kernel for the route to the arm. No RoArm request."""
    try:
        completed = subprocess.run(
            ['ip', '-4', 'route', 'get', ARM_HOST],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    if completed.returncode != 0:
        return completed.stderr or completed.stdout or 'ip route get failed'
    return completed.stdout


def interpret_route(text):
    """Classify `ip route get` output. A missing route stays unavailable."""
    device = None
    source = None
    tokens = str(text).split()
    for index, token in enumerate(tokens[:-1]):
        if token == 'dev':
            device = tokens[index + 1]
        elif token == 'src':
            source = tokens[index + 1]
    route = {
        'destination': ARM_HOST,
        'device': device,
        'source': source,
        'required_device': ROUTE_DEVICE,
        'required_source': PI_SOURCE,
        'text': ' '.join(tokens),
    }
    if device is None:
        route['status'] = 'unavailable'
    elif device != ROUTE_DEVICE:
        route['status'] = 'wrong_interface'
    elif source != PI_SOURCE:
        route['status'] = 'wrong_source'
    else:
        route['status'] = 'ok'
    return route


def runtime_dir_from_config(roarm_cfg):
    """One configured runtime directory. Status, stop, and pid files live inside it."""
    if isinstance(roarm_cfg, dict) and roarm_cfg.get('runtime_dir'):
        return Path(roarm_cfg['runtime_dir'])
    return DEFAULT_RUNTIME_DIR


def transport_status_path(roarm_cfg=None):
    return runtime_dir_from_config(roarm_cfg) / 'transport-status.json'


def use_configured_status_path(roarm_cfg):
    """Point the read-only status probe at the shared runtime directory."""
    global STATUS_PATH
    STATUS_PATH = transport_status_path(roarm_cfg)


def read_transport_status(path=None):
    """Read the pattern command's status file. Missing or invalid is None."""
    status_path = STATUS_PATH if path is None else Path(path)
    try:
        loaded = json.loads(status_path.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def _whole_number(value, *, allow_zero):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0 or (value == 0 and not allow_zero):
        return None
    return value


def _event(value):
    if not isinstance(value, dict):
        return None
    detail = value.get('detail')
    return {
        'at': value.get('at') if isinstance(value.get('at'), str) else None,
        'sequence': _whole_number(value.get('sequence'), allow_zero=True),
        'stream_id': _whole_number(value.get('stream_id'), allow_zero=False),
        'pattern': value.get('pattern') if isinstance(value.get('pattern'), str) else None,
        'detail': detail if isinstance(detail, str) else None,
    }


def _normalize_record(record):
    if not isinstance(record, dict):
        return None
    state = record.get('transport_state')
    serial_rejected = (
        state == 'serial'
        or record.get('serial_fallback') is True
        or record.get('serial_opened') is True
    )
    if state not in _TRANSPORT_STATES:
        state = None
    active_failure = record.get('active_failure')
    if active_failure not in _ACTIVE_FAILURES:
        active_failure = None
    detail = record.get('failure_detail')
    return {
        'transport_state': state,
        'active': record.get('active') is True and state in ('http', 'udp'),
        'stream_id': _whole_number(record.get('stream_id'), allow_zero=False),
        'last_sequence': _whole_number(record.get('last_sequence'), allow_zero=True),
        'last_completion': _event(record.get('last_completion')),
        'last_late': _event(record.get('last_late')),
        'last_failed': _event(record.get('last_failed')),
        'last_watchdog': _event(record.get('last_watchdog')),
        'active_failure': active_failure,
        'failure_detail': detail if isinstance(detail, str) and detail else None,
        'serial_rejected': serial_rejected,
        'updated_at': record.get('updated_at') if isinstance(record.get('updated_at'), str) else None,
    }


def _status_is_fresh(updated_at):
    """Freshness belongs to the status file clock, not the T105 sample."""
    elapsed = age(updated_at, utcnow())
    if elapsed is None or elapsed > TRANSPORT_STATUS_FRESH_S:
        return elapsed, False
    return elapsed, True


def _apply_communication(observed, route, record):
    """Attach link facts without turning a T105 failure into a serial path."""
    normalized = _normalize_record(record)
    connected = observed.get('connected') is True
    observed = dict(observed)
    observed['reachability'] = 'reachable' if connected else 'unreachable'
    observed['route'] = route
    observed['t105_fresh'] = connected and observed.get('fresh') is True
    observed['t105_observed_at'] = observed.get('observed_at') if connected else None
    observed['udp_target'] = UDP_TARGET
    observed['serial_fallback'] = False
    if normalized is None:
        observed.update(
            transport_state=None,
            stream_id=None,
            stream_role=None,
            last_sequence=None,
            last_completion=None,
            last_late=None,
            last_failed=None,
            last_watchdog=None,
            transport_status_updated_at=None,
            transport_status_age_s=None,
            transport_status_fresh=None,
        )
    else:
        updated_at = normalized['updated_at']
        status_age, status_fresh = _status_is_fresh(updated_at)
        current_udp = (
            status_fresh
            and normalized['active']
            and normalized['transport_state'] == 'udp'
            and not normalized['serial_rejected']
        )
        stream_id = normalized['stream_id']
        if current_udp:
            stream_role = 'current'
            transport_state = 'udp'
            last_sequence = None
        else:
            stream_role = 'last' if stream_id is not None else None
            last_sequence = (
                None
                if status_fresh and normalized['active']
                else normalized['last_sequence']
            )
            if normalized['serial_rejected'] or not status_fresh:
                transport_state = None
            elif normalized['active'] and normalized['transport_state'] == 'udp':
                transport_state = None
            else:
                transport_state = normalized['transport_state']
        observed.update(
            transport_state=transport_state,
            stream_id=stream_id,
            stream_role=stream_role,
            last_sequence=last_sequence,
            last_completion=normalized['last_completion'],
            last_late=normalized['last_late'],
            last_failed=normalized['last_failed'],
            last_watchdog=normalized['last_watchdog'],
            transport_status_updated_at=updated_at,
            transport_status_age_s=status_age,
            transport_status_fresh=status_fresh,
        )
    if route['status'] == 'unavailable':
        reason = (
            'RoArm route probe is unavailable; expected %s from %s to %s'
            % (ROUTE_DEVICE, PI_SOURCE, ARM_HOST)
        )
        if not connected and observed.get('error'):
            reason += '. RoArm unreachable: ' + str(observed.get('error'))
        observed.update(
            status='yellow',
            failure_reason=reason,
            **{'class': 'ARM_ROUTE_UNAVAILABLE'},
        )
        return observed
    if not connected:
        reason = 'RoArm unreachable'
        error = observed.get('error')
        if error:
            reason = 'RoArm unreachable: ' + str(error)
        if route['status'] == 'wrong_interface':
            reason += '. Route device is %s, expected %s' % (
                route.get('device') or 'unknown', ROUTE_DEVICE)
        elif route['status'] == 'wrong_source':
            reason += '. Route source is %s, expected %s' % (
                route.get('source') or 'unknown', PI_SOURCE)
        observed['failure_reason'] = reason
        return observed
    if normalized is not None and normalized['serial_rejected']:
        observed.update(
            status='yellow',
            transport_state=None,
            failure_reason='USB/serial is not a RoArm runtime path',
            **{'class': 'ROARM_SERIAL_REJECTED'},
        )
        return observed
    if route['status'] == 'wrong_interface':
        observed.update(
            status='yellow',
            failure_reason=(
                'Route device is %s; RoArm requires %s from %s to %s'
                % (route.get('device') or 'unknown', ROUTE_DEVICE, PI_SOURCE, ARM_HOST)
            ),
            **{'class': 'ARM_ROUTE_WRONG_INTERFACE'},
        )
        return observed
    if route['status'] == 'wrong_source':
        observed.update(
            status='yellow',
            failure_reason=(
                'Route source is %s; RoArm requires %s from %s to %s'
                % (route.get('source') or 'unknown', ROUTE_DEVICE, PI_SOURCE, ARM_HOST)
            ),
            **{'class': 'ARM_ROUTE_WRONG_SOURCE'},
        )
        return observed
    failure = None if normalized is None else normalized['active_failure']
    if failure:
        detail = normalized['failure_detail'] or failure
        observed.update(status='yellow', failure_reason=detail, **{'class': failure})
        return observed
    observed['failure_reason'] = None
    return observed


def probe_roarm(base_url, timeout_s=1.5):
    """Fetch one RoArm state packet and the local link facts around it."""
    endpoint = str(base_url).rstrip('/')
    route = interpret_route(read_arm_route())
    record = read_transport_status()
    try:
        query = urlencode({'json': '{"T":105}'})
        with urlopen(f'{endpoint}/js?{query}', timeout=timeout_s) as response:
            packet = json.loads(response.read().decode('utf-8'))

        if not isinstance(packet, dict):
            raise ValueError('RoArm response must be a JSON object')
        if packet.get('T') != 1051:
            raise ValueError('RoArm response must have T=1051')

        observed = {
            'status': 'green',
            'class': None,
            'connected': True,
            'fresh': True,
            'transport': 'http',
            'endpoint': endpoint,
            'observed_at': stamp(utcnow()),
            'pose': {
                'x': packet.get('x'),
                'y': packet.get('y'),
                'z': packet.get('z'),
                'tilt': packet.get('tit'),
            },
            'joints': {
                'base': packet.get('b'),
                'shoulder': packet.get('s'),
                'elbow': packet.get('e'),
                'wrist': packet.get('t'),
                'roll': packet.get('r'),
                'gripper': packet.get('g'),
            },
            'additional_feedback': {
                key: value
                for key, value in packet.items()
                if key not in {
                    'T', 'x', 'y', 'z', 'tit',
                    'b', 's', 'e', 't', 'r', 'g',
                }
            },
            'raw_feedback': packet,
        }
    except Exception as exc:
        observed = unknown_roarm(endpoint, exc)
    return _apply_communication(observed, route, record)
