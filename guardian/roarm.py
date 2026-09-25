"""Read-only RoArm state probe.

The only firmware command issued by this module is T=105.
"""
import json
from urllib.parse import urlencode
from urllib.request import urlopen

from .model import stamp, utcnow


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


def probe_roarm(base_url, timeout_s=1.5):
    """Fetch one RoArm state packet without exposing a command API."""
    endpoint = str(base_url).rstrip('/')
    try:
        query = urlencode({'json': '{"T":105}'})
        with urlopen(f'{endpoint}/js?{query}', timeout=timeout_s) as response:
            packet = json.loads(response.read().decode('utf-8'))

        if not isinstance(packet, dict):
            raise ValueError('RoArm response must be a JSON object')
        if packet.get('T') != 1051:
            raise ValueError('RoArm response must have T=1051')

        return {
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
        return unknown_roarm(endpoint, exc)
