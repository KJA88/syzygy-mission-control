import inspect
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from guardian.aggregator import Guardian
from guardian.config import load_config
from guardian.state_engine import build_state
from guardian.storage import changes
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
        self.assertFalse(configured['serial_fallback'])
        self.assertEqual(configured['udp_host'], roarm.ARM_HOST)
        self.assertEqual(configured['udp_port'], roarm.UDP_PORT)
        self.assertEqual(configured['route_device'], roarm.ROUTE_DEVICE)
        self.assertEqual(configured['route_source'], roarm.PI_SOURCE)
        self.assertEqual(configured['route_destination'], roarm.ARM_HOST)

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

    def test_transport_fault_does_not_change_system_status(self):
        observed = {
            'status': 'yellow',
            'class': 'PATTERN_UDP_LATE',
            'failure_reason': 'sequence 2 is behind by 0.100s',
            'connected': True,
        }
        with patch('guardian.aggregator.probe_roarm', return_value=observed):
            snapshot = Guardian(self.cfg).tick()
        self.assertEqual(snapshot['roarm']['class'], 'PATTERN_UDP_LATE')
        self.assertEqual(snapshot['system']['status'], 'green')


GOOD_ROUTE = '192.168.4.1 dev wlan0 src 192.168.4.2'


class RoArmCommunicationTests(unittest.TestCase):
    def probe(self, payload=None, route=GOOD_ROUTE, record=None, error=None):
        response = MockResponse(json.dumps(payload or {'T': 1051, 'z': 551.31}).encode('utf-8'))
        with patch('guardian.roarm.read_arm_route', return_value=route), \
                patch('guardian.roarm.read_transport_status', return_value=record), \
                patch('guardian.roarm.urlopen', return_value=response, side_effect=error):
            return roarm.probe_roarm('http://192.168.4.1')

    def test_reachable_direct_route_is_idle_without_a_status_file(self):
        result = self.probe()
        self.assertEqual(result['reachability'], 'reachable')
        self.assertTrue(result['t105_fresh'])
        self.assertEqual(result['route']['status'], 'ok')
        self.assertEqual(result['route']['device'], 'wlan0')
        self.assertEqual(result['route']['source'], '192.168.4.2')
        self.assertEqual(result['udp_target'], '192.168.4.1:4210')
        self.assertIsNone(result['transport_state'])
        self.assertIsNone(result['stream_id'])
        self.assertIsNone(result['last_sequence'])
        self.assertIsNone(result['last_watchdog'])
        self.assertFalse(result['serial_fallback'])
        self.assertEqual(result['status'], 'green')
        self.assertIsNone(result['failure_reason'])

    def test_wrong_interface_is_visible_and_optional(self):
        result = self.probe(route='192.168.4.1 dev eth0 src 192.168.4.2')
        self.assertEqual(result['status'], 'yellow')
        self.assertEqual(result['class'], 'ARM_ROUTE_WRONG_INTERFACE')
        self.assertIn('wlan0', result['failure_reason'])
        self.assertEqual(result['reachability'], 'reachable')

    def test_udp_status_file_reports_stream_without_serial(self):
        result = self.probe(record={
            'transport_state': 'udp',
            'active': True,
            'stream_id': 7,
            'last_sequence': 12,
            'udp_target': '192.168.4.1:4210',
            'serial_fallback': False,
            'last_completion': {
                'at': '2026-09-27T03:00:00Z',
                'pattern': 'lissajous',
                'stream_id': 4,
                'sequence': 300,
            },
            'last_late': {'at': '2026-09-27T02:00:00Z', 'sequence': 2, 'detail': 'behind'},
            'last_watchdog': None,
        })
        self.assertEqual(result['transport_state'], 'udp')
        self.assertEqual(result['stream_id'], 7)
        self.assertEqual(result['stream_role'], 'current')
        self.assertEqual(result['last_sequence'], 12)
        self.assertEqual(result['last_completion']['pattern'], 'lissajous')
        self.assertEqual(result['last_late']['sequence'], 2)
        self.assertIsNone(result['last_watchdog'])
        self.assertFalse(result['serial_fallback'])
        self.assertEqual(result['status'], 'green')

    def test_active_udp_failure_is_the_card_reason(self):
        result = self.probe(record={
            'transport_state': 'idle',
            'active': False,
            'stream_id': 9,
            'last_sequence': 0,
            'active_failure': 'PATTERN_UDP_LATE',
            'failure_detail': 'sequence 1 is behind by 0.100s',
            'last_late': {'sequence': 0, 'detail': 'sequence 1 is behind by 0.100s'},
        })
        self.assertEqual(result['status'], 'yellow')
        self.assertEqual(result['class'], 'PATTERN_UDP_LATE')
        self.assertEqual(result['failure_reason'], 'sequence 1 is behind by 0.100s')
        self.assertEqual(result['stream_role'], 'last')

    def test_serial_record_is_rejected(self):
        result = self.probe(record={
            'transport_state': 'serial',
            'serial_opened': True,
            'stream_id': 3,
        })
        self.assertEqual(result['status'], 'yellow')
        self.assertEqual(result['class'], 'ROARM_SERIAL_REJECTED')
        self.assertIsNone(result['transport_state'])
        self.assertFalse(result['serial_fallback'])
        self.assertIn('USB/serial', result['failure_reason'])

    def test_unreachable_names_the_route_when_it_is_wrong(self):
        result = self.probe(
            route='192.168.4.1 dev eth0 src 192.168.1.18',
            error=TimeoutError('timed out'),
        )
        self.assertEqual(result['reachability'], 'unreachable')
        self.assertEqual(result['class'], 'ROARM_UNREACHABLE')
        self.assertFalse(result['t105_fresh'])
        self.assertIn('timed out', result['failure_reason'])
        self.assertIn('eth0', result['failure_reason'])

    def test_state_engine_keeps_udp_target_configured(self):
        observed = self.probe(record={
            'transport_state': 'idle',
            'active': False,
            'stream_id': 7,
            'last_sequence': 300,
            'last_completion': {'pattern': 'lissajous', 'stream_id': 7, 'sequence': 300},
        })
        state = build_state({'trace_id': 'trace', 'roarm': observed})
        entity = next(item for item in state['entities'] if item['id'] == 'device/roarm')
        attributes = entity['attributes']
        self.assertEqual(attributes['reachability']['value'], 'reachable')
        self.assertEqual(attributes['reachability']['knowledge'], 'observed')
        self.assertEqual(attributes['udp_target']['knowledge'], 'configured')
        self.assertEqual(attributes['udp_target']['value'], '192.168.4.1:4210')
        self.assertEqual(attributes['stream_id']['value'], 7)
        self.assertEqual(attributes['last_sequence']['value'], 300)
        self.assertIsNone(attributes['watchdog_release']['value'])
        self.assertEqual(attributes['watchdog_release']['knowledge'], 'unknown')
        self.assertFalse(attributes['serial_fallback']['value'])

    def test_failure_becomes_a_guardian_event(self):
        current = {
            'generated_at': '2026-09-27T03:00:00Z',
            'trace_id': 'trace',
            'system': {'status': 'green'},
            'roarm': {
                'status': 'yellow',
                'class': 'PATTERN_UDP_FAILED',
                'failure_reason': 'short trajectory datagram',
            },
        }
        events = changes({'system': {'status': 'green'}}, current)
        roarm_events = [event for event in events if event['component'] == 'roarm']
        self.assertEqual(len(roarm_events), 1)
        self.assertEqual(roarm_events[0]['class'], 'PATTERN_UDP_FAILED')
        self.assertEqual(roarm_events[0]['message'], 'short trajectory datagram')

    def test_mission_control_renders_the_link_and_hides_serial(self):
        source = Path('ui/app.js').read_text(encoding='utf-8')
        for label in (
            'Reachability',
            'Route',
            'T105',
            'Transport state',
            'UDP target',
            'Stream ID',
            'Last sequence',
            'Last completion',
            'UDP late send',
            'UDP failed send',
            'Watchdog release',
            'Failure',
            'UDP trajectory active',
        ):
            self.assertIn(label, source)
        self.assertIn('if (state === "serial" || state === "usb") return "unavailable";', source)


if __name__ == '__main__':
    unittest.main()
