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
        for required, expected in [(False, 'yellow'), (True, 'red')]:
            obj = reduce_object('svc', True, {'local': fact('green', True, NOW, 't'), 'public': fact('red', required, NOW, 't')}, NOW, 't')
            self.assertEqual(obj['status'], expected)
        for required, expected in [(False, 'green'), (True, 'unknown')]:
            self.assertEqual(reduce_object('svc', True, {'auth': fact('unknown', required, NOW, 't')}, NOW, 't')['status'], expected)

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

    def test_missing_and_stale_agent_never_green_services(self):
        for evidence in ({}, {'pi': dict(observed_at=stamp(NOW - 91), services=[], paths=[])}):
            snapshot = build_snapshot(self.cfg, evidence, {}, NOW, 't')
            self.assertTrue(all(s['status'] == 'unknown' for s in snapshot['services']))
            self.assertEqual(snapshot['system']['status'], 'green')  # Existing all-optional policy.
        self.cfg['nodes'][0]['required'] = True
        self.assertEqual(build_snapshot(self.cfg, {}, {}, NOW, 't')['system']['status'], 'unknown')

    def test_camera_evidence_and_non_authoritative_unit(self):
        svc = next(s for s in self.cfg['services'] if s['id'] == 'dhras-vision-service')
        layers = {name: fact('green', True, NOW, 't') for name in ('local_process', 'local_port', 'local_health')}
        layers['local_service'] = fact('red', False, NOW, 't', 'SVC_INACTIVE')
        for camera in svc['cameras']:
            layers['camera/' + camera['id']] = camera_health({'online': camera['id'] != 'frontyard'}, True, 't')
            layers['camera/' + camera['id']]['observed_at'] = stamp(NOW)
        evidence = {'jetson': dict(observed_at=stamp(NOW), services=[dict(id=svc['id'], layers=layers)], paths=[])}
        snapshot = build_snapshot(self.cfg, evidence, {}, NOW, 't')
        actual = next(s for s in snapshot['services'] if s['id'] == svc['id'])
        self.assertEqual(actual['status'], 'red')
        self.assertEqual(actual['cameras']['frontyard']['status'], 'red')
        self.assertEqual(actual['cameras']['backyard']['status'], 'green')
        self.assertFalse(actual['layers']['local_service']['required'])
        layers['camera/frontyard']['status'] = 'green'
        snapshot = build_snapshot(self.cfg, evidence, {}, NOW, 't')
        self.assertEqual(next(s for s in snapshot['services'] if s['id'] == svc['id'])['status'], 'yellow')

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
            atomic_json(path, {'guardian': {'heartbeat_at': stamp(NOW), 'last_run_result': 'ok'}, 'system': {'status': 'green'}})
            self.assertEqual(read_snapshot(path, NOW + 120)['system']['status'], 'green')
            self.assertEqual(read_snapshot(path, NOW + 121)['system']['status'], 'unknown')

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
