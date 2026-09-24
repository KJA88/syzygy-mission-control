"""Dedicated Guardian authentication probes.

Credentials are read only from the Guardian process environment. Nothing in this
module writes, logs, or returns credential values.
"""
import os
import urllib.error

from .model import fact, utcnow
from .probes import mcp


def auth_mcp(service, url, trace, environ=None, transport=None):
    """Probe an MCP endpoint using the dedicated Guardian identity.

    V0.2 supports Cloudflare Access service-token headers. The probe is bounded to
    MCP initialize + notifications/initialized + tools/list through ``mcp``; it
    never performs tools/call.
    """
    env = os.environ if environ is None else environ
    required = bool(service.get('auth_required', False))
    mode = (env.get('SYZYGY_AUTH_PROBE_MODE') or 'off').strip().lower()

    if mode in ('', 'off', 'disabled', 'false', '0'):
        return fact('unknown', required, utcnow(), trace, 'AUTH_PROBE_OFF',
                    probe_mode='off', identity_configured=False)

    if mode != 'cloudflare_access':
        return fact('unknown', required, utcnow(), trace, 'AUTH_PROBE_OFF',
                    probe_mode=mode, identity_configured=False,
                    reason='UNSUPPORTED_AUTH_MODE')

    client_id = env.get('SYZYGY_CF_ACCESS_CLIENT_ID')
    client_secret = env.get('SYZYGY_CF_ACCESS_CLIENT_SECRET')
    if not client_id or not client_secret:
        return fact('unknown', required, utcnow(), trace, 'AUTH_PROBE_OFF',
                    probe_mode=mode, identity_configured=False,
                    reason='IDENTITY_MISSING')

    kwargs = dict(extra_headers={
        'CF-Access-Client-Id': client_id,
        'CF-Access-Client-Secret': client_secret,
    })
    if transport is not None:
        kwargs['transport'] = transport
    health, _ = mcp(service, url, required, trace, **kwargs)
    health['probe_mode'] = mode
    health['identity_configured'] = True
    health['probe_scope'] = 'authenticated_mcp_handshake'

    # For the dedicated identity, a 401/403 is specifically an auth failure,
    # rather than a generic public-endpoint failure.
    if health.get('http_status') in (401, 403):
        health['class'] = 'OAUTH_EXPIRED'
        health['status'] = 'red'
    return health
