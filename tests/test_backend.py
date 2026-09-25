import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from guardian.model import fact, fresh, reduce_object, reduce_system, stamp
from guardian.storage import atomic_json, append_events, changes, read_snapshot
from guardian.config import load_config
from guardian.aggregator import build_snapshot
from guardian.probes import mcp
from agents.local import camera_health

NOW = 1800000000


class ReducerTests(unittest.TestCase):
    def test_locked_fixtures(self):
        for folder in sorted((Path(__file__).parent / 'fixtures').glob('[0-9]*')):
            with self.subTest(case=folder.name):
                data = json.loads((folder / 'input.json').read_text())
                expected = json.loads((folder / 'expected.json').read_text())
                layers = {name: fact(values[0], values[1], NOW, 'test', values[2] if len(values) > 2 else None) for name, values in data['layers'].items()}
                obj = reduce_object('service', True, layers, NOW, 'test')
                self.assertEqual(obj['status'], expected['status'])
                self.assertEqual(obj['class'], expected['class'])
                self.assertEqual(list(obj['layers']), expected['layers'])
                guardian = dict(heartbeat_at=stamp(NOW - data.get('heartbeat_age', 0)), last_run_result='crash' if data.get('crashed') else 'ok')
                self.assertEqual(reduce_system([obj], guardian, NOW)[0], expected['system'])
                if data.get('previous'):
                    previous = {'services': [dict(obj, status=data['previous'])]}
                    current = dict(services=[obj], trace_id='test', generated_at=stamp(NOW))
                    self.assertTrue(any(e['class'] == 'RECOVERED' for e in changes(previous, current)))

    def test_required_public_failure_and_auth_off(self):
        for required, expected in [(False, 'green'), (True, 'red')]:
            obj = reduce_object('svc', True, {'local': fact('green', True, NOW, 't'), 'public': fact('red', required, NOW, 't')}, NOW, 't')
            self.assertEqual(obj['status'], expected)
        for required, expected in [(False, 'green'), (True, 'unknown')]:
            self.assertEqual(reduce_object('svc', True, {'auth': fact('unknown', required, NOW, 't')}, NOW, 't')['status'], expected)

    def test_optional_yellow_still_marks_soft_degradation(self):
        obj = reduce_object('svc', True, {
            'local': fact('green', True, NOW, 't'),
            'catalog': fact('yellow', False, NOW, 't', 'MCP_CATALOG_STALE'),
        }, NOW, 't')
        self.assertEqual(obj['status'], 'yellow')
        self.assertEqual(obj['class'], 'MCP_CATALOG_STALE')

    def test_precedence_and_stale(self):
        objects = [fact('unknown', True, NOW, 't'), fact('red', True, NOW, 't')]
        guardian = dict(heartbeat_at=stamp(NOW), last_run_result='ok')
        self.assertEqual(reduce_system(objects, guardian, NOW)[0], 'red')
        self.assertEqual(fresh(fact('green', True, NOW - 91, 't'), NOW)['status'], 'unknown')
        self.assertEqual(fresh(fact('green', True, NOW + 60, 't'), NOW)['status'], 'unknown')
        self.assertEqual(reduce_system([fact('red', False, NOW, 't')], guardian, NOW)[0], 'green')


class AggregatorTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config('config/services.yaml')

    def test_state_engine_is_an_additive_snapshot_section(self):
        snapshot = build_snapshot(self.cfg, {}, {}, NOW, 't')
        self.assertTrue({
            'schema_version', 'generated_at', 'trace_id', 'guardian', 'system',
            'nodes', 'services', 'paths', 'auth', 'activity',
        }.issubset(snapshot))
        self.assertEqual(snapshot['state_engine']['schema_version'], 1)
        self.assertEqual(snapshot['state_engine']['trace_id'], snapshot['trace_id'])
        self.assertTrue(snapshot['state_engine']['read_only'])

    def test_missing_and_stale_agent_never_green_services(self):
        for evidence in ({}, {'pi': dict(observed_at=stamp(NOW - 91), services=[], paths=[])}):
            snapshot = build_snapshot(self.cfg, evidence, {}, NOW, 't')
            self.assertTrue(all(s['status'] == 'unknown' for s in snapshot['services']))
            self.assertEqual(snapshot['system']['status'], 'unknown')

    def test_optional_frontyard_failure_does_not_degrade_required_vision(self):
        svc = next(s for s in self.cfg['services'] if s['id'] == 'dhras-vision-service')
        layers = {name: fact('green', True, NOW, 't') for name in ('local_service', 'local_port', 'local_health')}
        for camera in svc['cameras']:
            layers['camera/' + camera['id']] = camera_health(
                {'online': camera['id'] != 'frontyard'}, camera['required'], 't'
            )
            layers['camera/' + camera['id']]['observed_at'] = stamp(NOW)
        evidence = {'jetson': dict(observed_at=stamp(NOW), services=[dict(id=svc['id'], layers=layers)], paths=[])}
        snapshot = build_snapshot(self.cfg, evidence, {}, NOW, 't')
        actual = next(s for s in snapshot['services'] if s['id'] == svc['id'])
        self.assertEqual(actual['status'], 'green')
        self.assertTrue(actual['required'])
        self.assertEqual(actual['cameras']['frontyard']['status'], 'red')
        self.assertFalse(actual['cameras']['frontyard']['required'])
        self.assertEqual(actual['cameras']['backyard']['status'], 'green')
        self.assertEqual(actual['cameras']['indoor']['status'], 'green')
        self.assertTrue(actual['layers']['local_service']['required'])

    def test_unknown_camera_is_not_online(self):
        for value in ({}, {'online': 'false'}, None):
            self.assertEqual(camera_health(value, True, 't')['status'], 'unknown')


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.service = dict(timeout_s=1, expected_tools=1, probe=dict(protocol_version='2024-11-05', session_required=True))
        self.calls = []

    def transport(self, url, timeout, payload=None, headers=None, rpc_id=None):
        self.calls.append((copy.deepcopy(payload), copy.deepcopy(headers)))
        method = payload['method']
        if method == 'initialize':
            return {'jsonrpc': '2.0', 'id': 1, 'result': {'serverInfo': {'name': 'test'}, 'capabilities': {}, 'protocolVersion': '2024-11-05'}}, {'Mcp-Session-Id': 'session'}
        if method == 'notifications/initialized':
            return None, {}
        return {'jsonrpc': '2.0', 'id': rpc_id, 'result': {'tools': [{'name': 'safe'}]}}, {}

    def test_only_protocol_methods_and_session(self):
        health, catalog = mcp(self.service, 'http://example/mcp', True, 't', self.transport)
        self.assertEqual((health['status'], catalog['status']), ('green', 'green'))
        self.assertEqual([c[0]['method'] for c in self.calls], ['initialize', 'notifications/initialized', 'tools/list'])
        self.assertEqual(self.calls[-1][1]['Mcp-Session-Id'], 'session')
        self.assertEqual(self.calls[-1][1]['MCP-Protocol-Version'], '2024-11-05')

    def test_catalog_mismatch(self):
        self.service['expected_tools'] = 2
        health, catalog = mcp(self.service, 'http://example/mcp', True, 't', self.transport)
        self.assertEqual((health['status'], catalog['status']), ('green', 'yellow'))

    def test_malformed_wrong_id_rpc_error_and_timeout(self):
        for value in ({}, {'jsonrpc': '2.0', 'id': 99, 'result': {}}, {'jsonrpc': '2.0', 'id': 1, 'error': {'code': -1}}):
            health, _ = mcp(self.service, 'http://example/mcp', True, 't', lambda *args: (value, {}))
            self.assertEqual(health['status'], 'red')
        def timeout(*args):
            raise TimeoutError()
        health, _ = mcp(self.service, 'http://example/mcp', True, 't', timeout)
        self.assertEqual(health['class'], 'TOOL_TIMEOUT')

    def test_pagination(self):
        def paged(url, timeout, payload, headers, rpc_id=None):
            if payload['method'] != 'tools/list':
                return self.transport(url, timeout, payload, headers, rpc_id)
            self.calls.append((copy.deepcopy(payload), copy.deepcopy(headers)))
            result = {'tools': [{'name': 'second' if 'cursor' in payload['params'] else 'first'}]}
            if 'cursor' not in payload['params']:
                result['nextCursor'] = 'next'
            return {'jsonrpc': '2.0', 'id': rpc_id, 'result': result}, {}
        self.service['expected_tools'] = 2
        self.assertEqual(mcp(self.service, 'http://example/mcp', True, 't', paged)[1]['status'], 'green')
        self.assertEqual(self.calls[-1][0]['params']['cursor'], 'next')


class StorageTests(unittest.TestCase):
    def test_saved_snapshot_goes_unknown_after_guardian_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'snapshot.json'
            atomic_json(path, {
                'guardian': {'heartbeat_at': stamp(NOW), 'last_run_result': 'ok'},
                'system': {'status': 'green'},
                'state_engine': {'entities': [
                    {'id': 'system/syzygy', 'attributes': {
                        'health': {'value': 'green', 'knowledge': 'derived',
                                   'freshness': 'fresh', 'confidence': 1.0,
                                   'source': 'guardian/reducer', 'trace_id': 't'}}},
                    {'id': 'node/pi', 'attributes': {
                        'health': {'value': 'green', 'knowledge': 'derived',
                                   'freshness': 'fresh', 'source': 'guardian/node/pi',
                                   'trace_id': 'node-t'},
                        'host': {'value': 'pi', 'knowledge': 'configured',
                                 'freshness': 'fresh'},
                        'requested_input': {
                            'value': 'HDMI 2', 'knowledge': 'requested',
                            'freshness': 'fresh', 'source': 'operator/request',
                            'observed_at': stamp(NOW), 'trace_id': 'request-t',
                            'confidence': 1.0, 'reason': 'OPERATOR_REQUEST'},
                        'remembered_input': {
                            'value': 'HDMI 1', 'knowledge': 'remembered',
                            'freshness': 'fresh', 'source': 'state/cache',
                            'observed_at': stamp(NOW - 30), 'trace_id': 'memory-t',
                            'confidence': 0.6, 'reason': 'LAST_KNOWN'},
                        'unknown_input': {
                            'value': None, 'knowledge': 'unknown',
                            'freshness': 'unknown', 'source': 'tv-state-adapter',
                            'observed_at': None, 'trace_id': 'unknown-t',
                            'confidence': None, 'reason': 'EVIDENCE_MISSING'}}},
                ]},
            })
            self.assertEqual(read_snapshot(path, NOW + 120)['system']['status'], 'green')
            stale = read_snapshot(path, NOW + 121)
            self.assertEqual(stale['system']['status'], 'unknown')
            entities = {item['id']: item for item in stale['state_engine']['entities']}
            system_health = entities['system/syzygy']['attributes']['health']
            self.assertEqual(system_health['knowledge'], 'unknown')
            self.assertIsNone(system_health['value'])
            self.assertEqual(system_health['freshness'], 'stale')
            node = entities['node/pi']['attributes']
            self.assertEqual(node['health']['freshness'], 'stale')
            self.assertEqual(node['health']['trace_id'], 'node-t')
            self.assertEqual(node['host']['freshness'], 'fresh')
            self.assertEqual(node['requested_input'], {
                'value': 'HDMI 2', 'knowledge': 'requested',
                'freshness': 'stale', 'source': 'operator/request',
                'observed_at': stamp(NOW), 'trace_id': 'request-t',
                'confidence': 1.0, 'reason': 'OPERATOR_REQUEST'})
            self.assertEqual(node['remembered_input'], {
                'value': 'HDMI 1', 'knowledge': 'remembered',
                'freshness': 'stale', 'source': 'state/cache',
                'observed_at': stamp(NOW - 30), 'trace_id': 'memory-t',
                'confidence': 0.6, 'reason': 'LAST_KNOWN'})
            self.assertEqual(node['unknown_input']['freshness'], 'unknown')

    def test_fresh_probe_crash_does_not_make_current_evidence_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'snapshot.json'
            atomic_json(path, {
                'guardian': {'heartbeat_at': stamp(NOW), 'last_run_result': 'crash'},
                'system': {'status': 'unknown'},
                'state_engine': {'entities': [
                    {'id': 'node/pi', 'attributes': {
                        'health': {'value': 'green', 'knowledge': 'derived',
                                   'freshness': 'fresh'}}},
                ]},
            })
            snapshot = read_snapshot(path, NOW)
            health = snapshot['state_engine']['entities'][0]['attributes']['health']
            self.assertEqual(snapshot['system']['reason'], 'PROBE_CRASH')
            self.assertEqual(health['freshness'], 'fresh')

    def test_atomic_failure_preserves_previous_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'snapshot.json'
            atomic_json(path, {'version': 1})
            with patch('guardian.storage.os.replace', side_effect=OSError('injected')):
                with self.assertRaises(OSError):
                    atomic_json(path, {'version': 2})
            self.assertEqual(json.loads(path.read_text()), {'version': 1})
            self.assertEqual(len(list(Path(directory).iterdir())), 1)
            atomic_json(path, {'version': 3})
            self.assertEqual(json.loads(path.read_text()), {'version': 3})

    def test_fsync_failure_does_not_replace_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'snapshot.json'
            atomic_json(path, {'version': 1})
            with patch('guardian.storage.os.fsync', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):
                    atomic_json(path, {'version': 2})
            self.assertEqual(json.loads(path.read_text()), {'version': 1})
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_append_keeps_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.jsonl'
            append_events(path, [{'a': 1}])
            append_events(path, [{'a': 2}])
            self.assertEqual([json.loads(x) for x in path.read_text().splitlines()], [{'a': 1}, {'a': 2}])


if __name__ == '__main__':
    unittest.main()
