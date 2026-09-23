"""Real loopback transport tests, including an SSE connection that stays open."""
from contextlib import contextmanager
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import unittest
from unittest.mock import patch
from guardian.probes import mcp, request, ProbeError
from guardian.aggregator import Guardian
from guardian.model import stamp, utcnow


@contextmanager
def server(mode='json'):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(payload)
            if payload['method'] == 'notifications/initialized':
                self.send_response(202)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if payload['method'] == 'initialize':
                result = dict(serverInfo={'name': 'mock'}, capabilities={}, protocolVersion='2024-11-05')
            else:
                if self.headers.get('Mcp-Session-Id') != 'mock-session':
                    self.send_error(400)
                    return
                result = {'tools': [{'name': 'test'}]}
            body = json.dumps({'jsonrpc': '2.0', 'id': payload['id'], 'result': result}).encode()
            self.send_response(200)
            self.send_header('Mcp-Session-Id', 'mock-session')
            if mode == 'sse':
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                self.wfile.write(b': heartbeat\r\n\r\nevent: message\r\ndata: ' + body + b'\r\n\r\n')
                self.wfile.flush()
                # No Content-Length, EOF or close: the client must consume the event.
            else:
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def do_GET(self):
            body = json.dumps(dict(schema_version=1, node_id='pi', observed_at=stamp(utcnow()), services=[], paths=[])).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(httpd.server_port), calls
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


class HTTPTests(unittest.TestCase):
    def test_json_and_persistent_sse(self):
        for mode in ('json', 'sse'):
            with self.subTest(mode=mode), server(mode) as (url, calls):
                service = dict(timeout_s=1, expected_tools=1, probe=dict(session_required=True))
                started = time.monotonic()
                health, catalog = mcp(service, url + '/mcp', True, 'test')
                self.assertEqual((health['status'], catalog['status']), ('green', 'green'))
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual([c['method'] for c in calls], ['initialize', 'notifications/initialized', 'tools/list'])

    def test_guardian_tick_uses_agent_and_preserves_catalog_cache_age(self):
        with server() as (url, calls):
            service = dict(id='test', host='pi', required=True, public_required=False,
                           public_url=url, public_mcp_path='/mcp', timeout_s=1, interval_s=60,
                           expected_tools=1, probe={}, dependencies=[])
            cfg = dict(nodes=[dict(id='pi', required=True, agent=url + '/health')], services=[service], paths=[])
            guardian = Guardian(cfg)
            first = guardian.tick()
            second = guardian.tick()
            self.assertEqual(first['nodes'][0]['status'], 'green')
            self.assertEqual(first['services'][0]['status'], 'unknown')  # No local service evidence.
            self.assertEqual(first['services'][0]['layers']['public_mcp']['status'], 'green')
            self.assertEqual(first['services'][0]['layers']['public_mcp']['observed_at'], second['services'][0]['layers']['public_mcp']['observed_at'])
            self.assertEqual(len(calls), 3)


if __name__ == '__main__':
    unittest.main()
