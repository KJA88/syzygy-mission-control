"""Dedicated Guardian authentication probes.

Credentials are read only from the Guardian process environment. Nothing in this
module writes, logs, or returns credential values.
"""
import copy
import os

from .model import fact, utcnow
from .probes import mcp, request_httpclient


def auth_mcp(service, url, trace, environ=None, transport=None):
    """Probe an MCP endpoint using the dedicated Guardian identity.

    V0.2 supports Cloudflare Access service-token headers. The probe is bounded to
    MCP initialize + notifications/initialized + tools/list through ``mcp``; it
    never performs tools/call.
    """
    required = bool(service.get('auth_required', False))
    policy = str(service.get('auth_probe', 'off')).strip().lower()
    if policy in ('', 'off', 'disabled', 'false', '0'):
        return fact('unknown', required, utcnow(), trace, 'AUTH_PROBE_OFF',
                    probe_mode='off', identity_configured=False)

    env = os.environ if environ is None else environ
    mode = (env.get('SYZYGY_AUTH_PROBE_MODE') or 'off').strip().lower()
    if mode in ('', 'off', 'disabled', 'false', '0'):
        return fact('unknown', required, utcnow(), trace, 'AUTH_PROBE_OFF',
                    probe_mode='off', identity_configured=False)

    if policy != 'guardian_identity' or mode != 'cloudflare_access':
        return fact('unknown', required, utcnow(), trace, 'AUTH_PROBE_OFF',
                    probe_mode=mode, identity_configured=False,
                    reason='UNSUPPORTED_AUTH_MODE')

    client_id = env.get('SYZYGY_CF_ACCESS_CLIENT_ID')
    client_secret = env.get('SYZYGY_CF_ACCESS_CLIENT_SECRET')
    if not client_id or not client_secret:
        return fact('unknown', required, utcnow(), trace, 'AUTH_PROBE_OFF',
                    probe_mode=mode, identity_configured=False,
                    reason='IDENTITY_MISSING')

    probe_service = copy.deepcopy(service)
    probe_service['timeout_s'] = service.get('auth_timeout_s', service['timeout_s'])
    probe = probe_service.setdefault('probe', {})
    if service.get('auth_protocol_version'):
        probe['protocol_version'] = service['auth_protocol_version']
    if 'auth_session_required' in service:
        probe['session_required'] = bool(service['auth_session_required'])

    kwargs = dict(
        transport=transport or request_httpclient,
        extra_headers={
            'CF-Access-Client-Id': client_id,
            'CF-Access-Client-Secret': client_secret,
        },
    )
    health, _ = mcp(probe_service, url, required, trace, **kwargs)
    health['probe_mode'] = mode
    health['identity_configured'] = True
    health['probe_scope'] = 'authenticated_mcp_handshake'

    if health.get('http_status') in (401, 403):
        health['class'] = 'AUTH_DENIED'
        health['status'] = 'red'
    return health
