import copy
import io
import unittest
import urllib.error

from guardian.auth import auth_mcp


class AuthProbeTests(unittest.TestCase):
    def setUp(self):
        self.service = {
            'auth_probe': 'guardian_identity',
            'auth_required': False,
            'timeout_s': 1,
            'expected_tools': 1,
            'probe': {'protocol_version': '2024-11-05', 'session_required': True},
        }
        self.calls = []

    def transport(self, url, timeout, payload=None, headers=None, rpc_id=None):
        self.calls.append((copy.deepcopy(payload), copy.deepcopy(headers)))
        method = payload['method']
        if method == 'initialize':
            return {
                'jsonrpc': '2.0', 'id': 1,
                'result': {
                    'serverInfo': {'name': 'test'},
                    'capabilities': {},
                    'protocolVersion': '2024-11-05',
                },
            }, {'Mcp-Session-Id': 'session'}
        if method == 'notifications/initialized':
            return None, {}
        return {
            'jsonrpc': '2.0', 'id': rpc_id,
            'result': {'tools': [{'name': 'safe'}]},
        }, {}

    def test_off_without_identity_is_honest_unknown(self):
        item = auth_mcp(self.service, 'https://example/mcp', 't', environ={})
        self.assertEqual(item['status'], 'unknown')
        self.assertEqual(item['class'], 'AUTH_PROBE_OFF')
        self.assertFalse(item['identity_configured'])

    def test_authenticated_probe_uses_headers_and_never_tools_call(self):
        env = {
            'SYZYGY_AUTH_PROBE_MODE': 'cloudflare_access',
            'SYZYGY_CF_ACCESS_CLIENT_ID': 'client-id-secret-value',
            'SYZYGY_CF_ACCESS_CLIENT_SECRET': 'client-secret-secret-value',
        }
        item = auth_mcp(self.service, 'https://example/mcp', 't', env, self.transport)
        self.assertEqual(item['status'], 'green')
        self.assertTrue(item['identity_configured'])
        methods = [call[0]['method'] for call in self.calls]
        self.assertEqual(methods, ['initialize', 'notifications/initialized', 'tools/list'])
        self.assertNotIn('tools/call', methods)
        for _, headers in self.calls:
            self.assertEqual(headers['CF-Access-Client-Id'], env['SYZYGY_CF_ACCESS_CLIENT_ID'])
            self.assertEqual(headers['CF-Access-Client-Secret'], env['SYZYGY_CF_ACCESS_CLIENT_SECRET'])
        self.assertNotIn(env['SYZYGY_CF_ACCESS_CLIENT_ID'], repr(item))
        self.assertNotIn(env['SYZYGY_CF_ACCESS_CLIENT_SECRET'], repr(item))

    def test_403_is_classified_as_auth_failure(self):
        def denied(url, timeout, payload=None, headers=None, rpc_id=None):
            raise urllib.error.HTTPError(url, 403, 'Forbidden', {}, io.BytesIO(b''))

        env = {
            'SYZYGY_AUTH_PROBE_MODE': 'cloudflare_access',
            'SYZYGY_CF_ACCESS_CLIENT_ID': 'id',
            'SYZYGY_CF_ACCESS_CLIENT_SECRET': 'secret',
        }
        item = auth_mcp(self.service, 'https://example/mcp', 't', env, denied)
        self.assertEqual(item['status'], 'red')
        self.assertEqual(item['class'], 'AUTH_DENIED')
        self.assertEqual(item['http_status'], 403)

    def test_service_policy_off_prevents_probe(self):
        self.service['auth_probe'] = 'off'
        env = {
            'SYZYGY_AUTH_PROBE_MODE': 'cloudflare_access',
            'SYZYGY_CF_ACCESS_CLIENT_ID': 'id',
            'SYZYGY_CF_ACCESS_CLIENT_SECRET': 'secret',
        }
        item = auth_mcp(self.service, 'https://example/mcp', 't', env, self.transport)
        self.assertEqual(item['status'], 'unknown')
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
