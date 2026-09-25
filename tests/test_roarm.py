import inspect
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from guardian.aggregator import Guardian
from guardian.config import load_config
import guardian.roarm as roarm


class MockResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class RoArmProbeTests(unittest.TestCase):
    def probe_with(self, payload):
        response = MockResponse(json.dumps(payload).encode('utf-8'))
        with patch('guardian.roarm.urlopen', return_value=response) as opener:
            result = roarm.probe_roarm('http://192.168.4.1/', timeout_s=1.5)
        return result, opener

    def test_url_contains_only_encoded_read_only_state_command(self):
        result, opener = self.probe_with({'T': 1051})
        url = opener.call_args.args[0]
        parsed = urlparse(url)
        self.assertEqual(parsed.path, '/js')
        self.assertEqual(parse_qs(parsed.query), {'json': ['{"T":105}']})
        self.assertIn('json=%7B%22T%22%3A105%7D', url)
        self.assertEqual(opener.call_args.kwargs, {'timeout': 1.5})
        self.assertEqual(result['status'], 'green')

    def test_valid_feedback_becomes_green_and_is_normalized(self):
        packet = {
            'T': 1051, 'x': 1, 'y': 2, 'z': 3, 'tit': 4,
            'b': 5, 's': 6, 'e': 7, 't': 8, 'r': 9, 'g': 10,
            'voltage': 12.1,
        }
        result, _ = self.probe_with(packet)
        self.assertEqual(result['status'], 'green')
        self.assertTrue(result['connected'])
        self.assertTrue(result['fresh'])
        self.assertEqual(result['endpoint'], 'http://192.168.4.1')
        self.assertEqual(result['pose'], {'x': 1, 'y': 2, 'z': 3, 'tilt': 4})
        self.assertEqual(result['joints']['gripper'], 10)
        self.assertEqual(result['additional_feedback'], {'voltage': 12.1})
        self.assertEqual(result['raw_feedback'], packet)

    def test_wrong_packet_type_becomes_unknown(self):
        result, _ = self.probe_with({'T': 1041})
        self.assertEqual(result['status'], 'unknown')
        self.assertFalse(result['connected'])
        self.assertFalse(result['fresh'])

    def test_malformed_json_becomes_unknown(self):
        response = MockResponse(b'not-json')
        with patch('guardian.roarm.urlopen', return_value=response):
            result = roarm.probe_roarm('http://192.168.4.1')
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['class'], 'ROARM_UNREACHABLE')

    def test_timeout_becomes_unknown(self):
        with patch('guardian.roarm.urlopen', side_effect=TimeoutError('timed out')):
            result = roarm.probe_roarm('http://192.168.4.1')
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['class'], 'ROARM_UNREACHABLE')

    def test_no_arbitrary_command_api_exists(self):
        self.assertFalse(hasattr(roarm, 'send_command'))
        self.assertEqual(
            list(inspect.signature(roarm.probe_roarm).parameters),
            ['base_url', 'timeout_s'],
        )
        source = Path(roarm.__file__).read_text(encoding='utf-8')
        self.assertEqual(source.count('{"T":105}'), 1)
        for command in ('T100', 'T101', 'T102', 'T104', 'T1041', 'T106'):
            self.assertNotIn(command, source)


class RoArmGuardianTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            'guardian': {},
            'nodes': [],
            'services': [],
            'paths': [],
            'roarm': {
                'id': 'roarm',
                'display_name': 'RoArm-M3',
                'required': False,
                'transport': 'http',
                'base_url': 'http://192.168.4.1',
                'timeout_s': 1.5,
            },
        }

    def test_config_keeps_roarm_optional(self):
        configured = load_config('config/services.yaml')['roarm']
        self.assertFalse(configured['required'])
        self.assertEqual(configured['transport'], 'http')

    def test_guardian_snapshot_includes_roarm(self):
        observed = roarm.unknown_roarm(
            'http://192.168.4.1',
            'mock unreachable',
        )
        with patch('guardian.aggregator.probe_roarm', return_value=observed):
            snapshot = Guardian(self.cfg).tick()
        self.assertEqual(snapshot['roarm'], observed)

    def test_optional_roarm_unknown_does_not_make_system_red(self):
        observed = roarm.unknown_roarm(
            'http://192.168.4.1',
            'mock unreachable',
        )
        with patch('guardian.aggregator.probe_roarm', return_value=observed):
            snapshot = Guardian(self.cfg).tick()
        self.assertEqual(snapshot['roarm']['status'], 'unknown')
        self.assertEqual(snapshot['system']['status'], 'green')


if __name__ == '__main__':
    unittest.main()
