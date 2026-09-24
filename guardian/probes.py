"""Bounded HTTP/MCP observations; there is deliberately no tools/call path."""
import hashlib
import http.client
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from .model import fact, utcnow

MAX_BYTES = 2 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ProbeError(Exception):
    def __init__(self, classification):
        self.classification = classification


def _read_response(response, timeout, payload=None, rpc_id=None):
    response_headers = response.headers
    if payload and 'id' not in payload:
        return None, response_headers
    sse = 'text/event-stream' in response_headers.get('Content-Type', '')
    deadline = time.monotonic() + timeout
    total, pending, chunks, raw = 0, b'', [], bytearray()
    while True:
        if time.monotonic() > deadline:
            raise ProbeError('TOOL_TIMEOUT')
        block = response.read1(8192)
        total += len(block)
        if total > MAX_BYTES:
            raise ProbeError('MCP_MALFORMED')
        if sse:
            pending += block
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                line = line.rstrip(b'\r')
                if line.startswith(b'data:'):
                    chunks.append(line[5:].lstrip())
                if not line and chunks:
                    value = json.loads(b'\n'.join(chunks))
                    chunks = []
                    if isinstance(value, dict) and value.get('id') == rpc_id:
                        return value, response_headers
        else:
            raw.extend(block)
        if not block:
            if sse:
                raise ProbeError('MCP_MALFORMED')
            return json.loads(raw), response_headers


def request(url, timeout, payload=None, headers=None, rpc_id=None):
    body = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers=headers or {})
    with urllib.request.build_opener(NoRedirect).open(req, timeout=timeout) as response:
        return _read_response(response, timeout, payload, rpc_id)


def request_httpclient(url, timeout, payload=None, headers=None, rpc_id=None):
    """HTTP transport that preserves caller-supplied header casing.

    Cloudflare Access service-token headers are sent exactly as configured. This
    transport is used only for the dedicated auth probe; ordinary probes retain
    urllib behavior.
    """
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('Unsupported URL')
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    conn_cls = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
    conn = conn_cls(parsed.hostname, port, timeout=timeout)
    path = urllib.parse.urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
    body = None if payload is None else json.dumps(payload).encode()
    try:
        conn.request('POST' if payload is not None else 'GET', path, body=body, headers=headers or {})
        response = conn.getresponse()
        if response.status >= 300:
            # Preserve the existing classifier contract used by mcp/auth_mcp.
            raise urllib.error.HTTPError(url, response.status, response.reason, response.headers, response)
        return _read_response(response, timeout, payload, rpc_id)
    finally:
        conn.close()


def classify(error):
    if isinstance(error, ProbeError):
        return error.classification
    if isinstance(error, (TimeoutError, socket.timeout)):
        return 'TOOL_TIMEOUT'
    if isinstance(error, urllib.error.HTTPError):
        return 'PUBLIC_ENDPOINT_FAIL'
    if isinstance(error, urllib.error.URLError):
        if isinstance(error.reason, ssl.SSLError):
            return 'TLS_FAIL'
        if isinstance(error.reason, socket.gaierror):
            return 'DNS_FAIL'
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            return 'TOOL_TIMEOUT'
        return 'MCP_HANDSHAKE_FAIL'
    if isinstance(error, (ValueError, KeyError, TypeError)):
        return 'MCP_MALFORMED'
    return 'PROBE_CRASH'


def result(value, rpc_id):
    if not isinstance(value, dict) or value.get('jsonrpc') != '2.0' or value.get('id') != rpc_id or 'error' in value or not isinstance(value.get('result'), dict):
        raise ProbeError('MCP_MALFORMED')
    return value['result']


def mcp(service, url, required, trace, transport=request, extra_headers=None):
    started = time.monotonic()
    deadline = started + service['timeout_s']

    def call(payload, headers, rpc_id=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProbeError('TOOL_TIMEOUT')
        return transport(url, remaining, payload, headers, rpc_id)

    catalog = fact('unknown', False, utcnow(), trace, 'MCP_CATALOG_MISSING')
    try:
        probe = service['probe']
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
        if extra_headers:
            headers.update(extra_headers)
        protocol = probe.get('protocol_version', '2024-11-05')
        value, received = call({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
                'protocolVersion': protocol, 'capabilities': {},
                'clientInfo': {'name': 'syzygy-guardian', 'version': '0.2.0'}}}, headers, 1)
        initialized = result(value, 1)
        info = initialized.get('serverInfo')
        if not isinstance(info, dict) or not isinstance(info.get('name'), str) or not isinstance(initialized.get('capabilities'), dict) or not isinstance(initialized.get('protocolVersion'), str):
            raise ProbeError('MCP_MALFORMED')
        if initialized['protocolVersion'] != protocol:
            raise ProbeError('MCP_HANDSHAKE_FAIL')
        session = received.get('Mcp-Session-Id')
        if probe.get('session_required') and not session:
            raise ProbeError('MCP_HANDSHAKE_FAIL')
        if session:
            headers['Mcp-Session-Id'] = session
        headers['MCP-Protocol-Version'] = initialized['protocolVersion']
        call({'jsonrpc': '2.0', 'method': 'notifications/initialized'}, headers)
        names, cursor, seen = [], None, set()
        for page in range(32):
            params = {} if cursor is None else {'cursor': cursor}
            value, _ = call({'jsonrpc': '2.0', 'id': page + 2, 'method': 'tools/list', 'params': params}, headers, page + 2)
            listing = result(value, page + 2)
            tools = listing.get('tools')
            if not isinstance(tools, list) or any(not isinstance(t, dict) or not isinstance(t.get('name'), str) for t in tools):
                raise ProbeError('MCP_MALFORMED')
            names.extend(t['name'] for t in tools)
            cursor = listing.get('nextCursor')
            if cursor is None:
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise ProbeError('MCP_MALFORMED')
            seen.add(cursor)
        else:
            raise ProbeError('MCP_MALFORMED')
        digest = hashlib.sha256('\n'.join(sorted(names)).encode()).hexdigest()
        mismatch = len(names) != service.get('expected_tools', len(names)) or len(names) != len(set(names))
        mismatch |= digest != service.get('expected_tool_names_sha256', digest)
        mismatch |= bool(probe.get('server_info', {}).get('name') and info['name'] != probe['server_info']['name'])
        mismatch |= bool(probe.get('server_info', {}).get('version') and info.get('version') != probe['server_info']['version'])
        catalog = fact('yellow' if mismatch else 'green', False, utcnow(), trace,
                       'MCP_CATALOG_STALE' if mismatch else None, count=len(names), names_sha256=digest)
        health = fact('green', required, utcnow(), trace, server_info=info)
    except Exception as error:
        cls = classify(error)
        health = fact('unknown' if cls == 'PROBE_CRASH' else 'red', required, utcnow(), trace, cls)
        if isinstance(error, urllib.error.HTTPError):
            health['http_status'] = error.code
    health['duration_ms'] = round((time.monotonic() - started) * 1000)
    return health, catalog


def http_json(url, timeout, required, trace):
    try:
        value, _ = request(url, timeout)
        if not isinstance(value, (dict, list)):
            raise ValueError('Expected JSON object or array')
        return fact('green', required, utcnow(), trace), value
    except Exception as error:
        cls = classify(error)
        return fact('unknown' if cls == 'PROBE_CRASH' else 'red', required, utcnow(), trace, cls), None
