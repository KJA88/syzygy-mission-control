"""Auth transport, total request budget, and Guardian routing regressions."""
import copy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from guardian.aggregator import Guardian
from guardian.auth import auth_mcp
from guardian.config import load_config
from guardian.probes import mcp
from test_http import server


ENV = {
    'SYZYGY_AUTH_PROBE_MODE': 'cloudflare_access',
    'SYZYGY_CF_ACCESS_CLIENT_ID': 'test-client',
    'SYZYGY_CF_ACCESS_CLIENT_SECRET': 'test-secret',
}


def reply(payload):
    if payload['method'] == 'notifications/initialized':
        return None
    result = ({'serverInfo': {'name': 'mock'}, 'capabilities': {},
               'protocolVersion': payload['params']['protocolVersion']}
              if payload['method'] == 'initialize' else {'tools': []})
    return {'jsonrpc': '2.0', 'id': payload['id'], 'result': result}


class AuthHardeningTests(unittest.TestCase):
    def setUp(self):
        cfg = load_config(Path(__file__).resolve().parents[1] / 'config/services.yaml')
        self.service = next(s for s in cfg['services'] if s['id'] == 'tv-mcp')

    def test_auth_budget_is_separate_shared_and_does_not_mutate_service(self):
        original = copy.deepcopy(self.service)
        self.assertEqual(self.service['timeout_s'], 5)
        self.assertEqual(self.service['auth_timeout_s'], 10)
        self.assertFalse(self.service['auth_required'])
        for authenticated, expected in ((True, [10, 7, 4]), (False, [5, 2])):
            with self.subTest(authenticated=authenticated):
                clock = [0]
                budgets, payloads = [], []

                def transport(url, timeout, payload, headers, rpc_id):
                    budgets.append(timeout)
                    payloads.append(copy.deepcopy(payload))
                    clock[0] += 3
                    return reply(payload), {'Mcp-Session-Id': 'session'}

                with patch('guardian.probes.time.monotonic', side_effect=lambda: clock[0]):
                    if authenticated:
                        health = auth_mcp(self.service, 'https://example/mcp', 't', ENV, transport)
                    else:
                        health, _ = mcp(self.service, 'https://example/mcp', False, 't', transport)
                self.assertEqual(budgets, expected)
                self.assertEqual(health['status'], 'green' if authenticated else 'red')
                if not authenticated:
                    self.assertEqual(health['class'], 'TOOL_TIMEOUT')
                self.assertEqual(payloads[0]['params']['protocolVersion'],
                                 self.service['auth_protocol_version'] if authenticated
                                 else self.service['probe']['protocol_version'])
        self.assertEqual(self.service, original)

    def test_auth_timeout_falls_back_and_exhausts_before_next_request(self):
        for override in (None, 10):
            with self.subTest(override=override):
                service = copy.deepcopy(self.service)
                service.pop('auth_timeout_s')
                if override is not None:
                    service['auth_timeout_s'] = override
                budget = override or service['timeout_s']
                clock, calls = [0], []

                def transport(url, timeout, payload, headers, rpc_id):
                    calls.append((payload['method'], timeout))
                    clock[0] += budget
                    return reply(payload), {}

                with patch('guardian.probes.time.monotonic', side_effect=lambda: clock[0]):
                    health = auth_mcp(service, 'https://example/mcp', 't', ENV, transport)
                self.assertEqual(calls, [('initialize', budget)])
                self.assertEqual(health['class'], 'TOOL_TIMEOUT')

    def test_httpclient_https_headers_path_session_and_cleanup(self):
        for content_type in ('application/json', 'text/event-stream'):
            with self.subTest(content_type=content_type):
                requests, connections = [], []

                class Connection:
                    def __init__(self, host, port, timeout):
                        self.closed = False
                        connections.append(self)
                        self.endpoint = (host, port, timeout)

                    def request(self, method, path, body, headers):
                        self.payload = json.loads(body)
                        requests.append((method, path, self.payload, dict(headers)))

                    def getresponse(self):
                        value = reply(self.payload)
                        body = json.dumps(value).encode()
                        if content_type == 'text/event-stream':
                            body = b'data: ' + body + b'\n\n'
                        response = io.BytesIO(body)
                        response.status = 202 if value is None else 200
                        response.headers = {'Content-Type': content_type, 'Mcp-Session-Id': 'session'}
                        return response

                    def close(self):
                        self.closed = True

                with patch('guardian.probes.http.client.HTTPSConnection', Connection), \
                        patch('guardian.probes.urllib.request.build_opener', side_effect=AssertionError('Wrong transport')):
                    health = auth_mcp(self.service, 'https://example:8443/mcp?route=tv', 't', ENV)
                self.assertEqual(health['status'], 'green')
                self.assertEqual([r[2]['method'] for r in requests],
                                 ['initialize', 'notifications/initialized', 'tools/list'])
                for method, path, payload, headers in requests:
                    self.assertEqual((method, path), ('POST', '/mcp?route=tv'))
                    self.assertEqual(headers['CF-Access-Client-Id'], ENV['SYZYGY_CF_ACCESS_CLIENT_ID'])
                    self.assertEqual(headers['CF-Access-Client-Secret'], ENV['SYZYGY_CF_ACCESS_CLIENT_SECRET'])
                    if payload['method'] != 'initialize':
                        self.assertEqual(headers['Mcp-Session-Id'], 'session')
                        self.assertEqual(headers['MCP-Protocol-Version'], '2025-06-18')
                self.assertTrue(all(c.closed for c in connections))
                self.assertTrue(all(c.endpoint[:2] == ('example', 8443) for c in connections))
                self.assertTrue(all(0 < c.endpoint[2] <= 10 for c in connections))
                self.assertNotIn('test-secret', repr(health))

    def test_httpclient_denials_redirects_and_timeout_close_connection(self):
        for code, expected in ((401, 'AUTH_DENIED'), (403, 'AUTH_DENIED'),
                               (302, 'PUBLIC_ENDPOINT_FAIL'), (500, 'PUBLIC_ENDPOINT_FAIL'),
                               (None, 'TOOL_TIMEOUT')):
            with self.subTest(code=code), patch('guardian.probes.http.client.HTTPSConnection') as factory:
                connection = factory.return_value
                if code is None:
                    connection.getresponse.side_effect = TimeoutError()
                else:
                    response = connection.getresponse.return_value
                    response.status, response.reason = code, 'test'
                    response.headers = {'Location': 'https://other.example/mcp'}
                health = auth_mcp(self.service, 'https://example/mcp', 't', ENV)
                self.assertEqual(health['class'], expected)
                self.assertEqual(health['status'], 'red')
                if code is not None:
                    self.assertEqual(health['http_status'], code)
                factory.assert_called_once()
                connection.request.assert_called_once()
                connection.close.assert_called_once()

    def test_httpclient_real_loopback_json_and_persistent_sse(self):
        for mode in ('json', 'sse'):
            with self.subTest(mode=mode), server(mode) as (url, calls):
                service = dict(auth_probe='guardian_identity', timeout_s=1,
                               probe={'session_required': True})
                health = auth_mcp(service, url + '/mcp', 't', ENV)
                self.assertEqual(health['status'], 'green')
                self.assertEqual([c['method'] for c in calls],
                                 ['initialize', 'notifications/initialized', 'tools/list'])

    def test_guardian_auth_url_override_and_configuration_fallback(self):
        for auth_url in ('https://portal.example/mcp?route=tv', None, '', 'VERIFY_LIVE'):
            with self.subTest(auth_url=auth_url):
                service = copy.deepcopy(self.service)
                service['public_url'] = 'https://tv.example/'
                service['public_mcp_path'] = '/mcp'
                service.pop('auth_url')
                if auth_url is not None:
                    service['auth_url'] = auth_url
                cfg = dict(nodes=[dict(id=service['host'], required=True)], services=[service], paths=[])
                health = dict(status='green', required=False, observed_at=None, **{'class': None})
                with patch('guardian.aggregator.agent_evidence', return_value=None), \
                        patch('guardian.aggregator.mcp', return_value=(health, health)) as public, \
                        patch('guardian.aggregator.auth_mcp', return_value=health) as auth:
                    Guardian(cfg).tick()
                self.assertEqual(public.call_args.args[1], 'https://tv.example/mcp')
                self.assertEqual(auth.call_args.args[1], auth_url if auth_url and auth_url.startswith('https://')
                                 else 'https://tv.example/mcp')
                self.assertEqual(auth.call_count, 1)

    def test_batch_enables_exactly_six_read_only_nonblocking_auth_routes(self):
        cfg = load_config(Path(__file__).resolve().parents[1] / 'config/services.yaml')
        services = [s for s in cfg['services'] if 'probe' in s]
        self.assertEqual({s['id'] for s in services}, {
            'dhras-mcp', 'fitbit-mcp', 'tv-mcp', 'pi-git-mcp',
            'polar-h10-mcp', 'jetson-git-mcp'})
        for service in services:
            with self.subTest(service=service['id']):
                self.assertEqual(service['auth_probe'], 'guardian_identity')
                self.assertEqual(service['auth_timeout_s'], 10)
                self.assertEqual(service['timeout_s'], 5)
                self.assertIs(service['auth_required'], False)
                self.assertIs(service['probe']['tool_calls_allowed'], False)
                self.assertFalse(service['probe'].get('functional_probe_enabled', False))
                if service['id'] != 'tv-mcp':
                    self.assertNotIn('auth_url', service)
                    self.assertNotIn('auth_protocol_version', service)
                    self.assertNotIn('auth_session_required', service)
                calls = []

                def transport(url, timeout, payload, headers, rpc_id):
                    calls.append(payload['method'])
                    return reply(payload), {'Mcp-Session-Id': 'session'}

                health = auth_mcp(service, service.get('auth_url') or
                                  service['public_url'] + service['public_mcp_path'],
                                  'batch-test', ENV, transport)
                self.assertEqual(health['status'], 'green')
                self.assertFalse(health['required'])
                self.assertEqual(calls, ['initialize', 'notifications/initialized', 'tools/list'])
                missing = auth_mcp(service, service['public_url'], 'batch-test', {})
                self.assertEqual(missing['class'], 'AUTH_PROBE_OFF')
                self.assertFalse(missing['required'])
        self.assertTrue(all(s.get('auth_probe', 'off') == 'off'
                            for s in cfg['services'] if 'probe' not in s))


if __name__ == '__main__':
    unittest.main()
