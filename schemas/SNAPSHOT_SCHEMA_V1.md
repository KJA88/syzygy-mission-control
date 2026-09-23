# Snapshot Schema V1

Top-level fields:

- `schema_version`
- `generated_at` (UTC)
- `trace_id`
- `guardian`
- `system`
- `nodes`
- `services`
- `paths`
- `auth`
- `activity`

Every asserted object should carry:

- `status`: `green|yellow|red|unknown`
- `required`: boolean
- `observed_at`: UTC timestamp or null
- `observation_age_s`: number or null
- `trace_id`
- `class`: failure/status classification or null

Recommended service layer chips:

- `host`
- `local_service`
- `local_mcp`
- `public_tls`
- `public_mcp`
- `portal_catalog`
- `client_invoke`

Each layer preserves its own result and age. Reducers never erase conflicting lower-level evidence.

Suggested failure classes include:

`HOST_UNREACHABLE`, `HOST_AGENT_DOWN`, `SVC_INACTIVE`, `SVC_ALIVE_UNHEALTHY`, `PORT_CLOSED`, `MCP_HANDSHAKE_FAIL`, `MCP_MALFORMED`, `MCP_CATALOG_MISSING`, `MCP_CATALOG_STALE`, `CF_TUNNEL_DOWN`, `DNS_FAIL`, `TLS_FAIL`, `PUBLIC_ENDPOINT_FAIL`, `PORTAL_ROUTING_FAIL`, `OAUTH_EXPIRED`, `OAUTH_SESSION_STALE`, `TOOL_INVOKE_FAIL`, `TOOL_TIMEOUT`, `DEPENDENCY_OUTAGE`, `NET_INTERMITTENT`, `RECOVERED`, `PROBE_CRASH`, `SNAPSHOT_STALE`, `CONFLICTING_PROBES`.

Stored timestamps are UTC. UI may render local time.
