# ADR-SYZ-MC-001 — Mission Control and Guardian Architecture

Status: Accepted for V0.1
Date: 2026-09-23
Owner: KJA

## Context

SYZYGY currently spans multiple hosts and services: Raspberry Pi, Jetson, DHRAS/Vision Hub, MCP servers, Cloudflare/public paths, authentication, Fitbit, TV, and operator activity. Diagnosing failures currently requires checking machines, processes, ports, tunnels, catalogs, and clients separately.

Mission Control will provide one read-only operational cockpit that reports system truth without becoming a second control plane.

RoArm is explicitly out of scope for V0.1.

## Decision

Create a separate repository: `KJA88/syzygy-mission-control`.

Use this architecture:

Pi local agent + Jetson local agent -> Guardian aggregator -> versioned snapshot + append-only events -> Mission Control UI.

The browser is a renderer only. It never probes hosts, MCP endpoints, Cloudflare, or authentication directly.

## Components

### Local agents

One lightweight read-only agent runs per monitored host.

Pi agent responsibilities:
- host metrics
- local process/service state
- listening ports
- local MCP reachability
- local Cloudflare process/unit observations

Jetson agent responsibilities:
- host/GPU metrics
- DHRAS/Vision service state
- listening ports
- camera/vision health endpoints
- local MCP reachability

### Guardian aggregator

The Guardian merges local-agent observations with public-path probes and produces the authoritative Mission Control snapshot.

Guardian probes follow layers rather than a single yes/no check:

host -> local service -> local port -> local MCP -> public DNS/TLS -> public MCP -> portal/catalog -> auth/client invoke

Each service preserves all layer results. Conflicting results are never averaged into GREEN.

### State outputs

Guardian writes:
- versioned `snapshot.json`
- append-only `events.jsonl`

Snapshot writes must be atomic: temp file -> fsync -> rename.

### UI

Mission Control reads the current snapshot and event stream. It does not perform infrastructure probes itself.

V0.1 is LAN-first.

## Health model

Statuses:
- `green`: required evidence is fresh and all required checks pass
- `yellow`: degraded or optional failure while required function remains available
- `red`: required component/layer failed
- `unknown`: fresh evidence is missing, stale, skipped, or the probe crashed

`UNKNOWN` is first-class. It is not treated as healthy and always blocks GREEN for required objects.

Overall status is reduced from objects configured with `required: true` only:
- any required RED -> overall RED
- else any required UNKNOWN -> overall UNKNOWN
- else any required YELLOW -> overall YELLOW
- else GREEN

## Heartbeat and staleness

Every observation carries `observed_at`, `observation_age_s`, and `trace_id`.

A stale Guardian heartbeat forces overall `unknown` regardless of the previous snapshot.

Initial policy targets:
- local-agent fresh: <= 60 s
- local-agent unknown: > 90 s
- aggregator unknown: > 120 s
- MCP observations display age; stale evidence never paints GREEN

Thresholds remain configurable.

## Required vs optional

Requiredness is configuration, not hard-coded reducer behavior.

Examples:
- Pi and Jetson may be required once their agents are deployed
- TV or Fitbit may be optional depending on product intent
- public path can be required per service when remote access is part of the service contract

## Authentication

Auth checks use a dedicated Guardian probe identity. Interactive ChatGPT/Cursor/Grok sessions are not authoritative health probes.

Until a dedicated identity exists, auth probes remain `off`/`unknown`. If a required auth layer is disabled, the service becomes `unknown`, never GREEN.

## Security

V0.1 is read-only.

No arbitrary shell execution is exposed through Mission Control. Local agents expose only bounded health/status data. Browser-to-host direct probing is prohibited. Credentials remain outside the repository.

## Consequences

Benefits:
- one source of operational truth
- failures classified by layer
- stale-green failure mode prevented
- infrastructure and future control systems remain separated
- future services can plug into the same status/event model

Costs:
- two host agents plus central aggregator
- explicit config and stale-evidence handling
- dedicated auth probe identity required for trustworthy end-to-end auth status

## Alternatives rejected

### Browser probes services directly
Rejected because it duplicates network/auth logic in the UI, creates inconsistent evidence, and makes trace correlation difficult.

### Central-only Guardian with no host agents
Rejected as the long-term design because it cannot reliably distinguish host-local failures from network visibility failures. It may be used only as a temporary spike.

### Put Mission Control in `RoArm-M3`
Rejected because Mission Control is SYZYGY infrastructure, not robot authority, and RoArm is not part of the current V0.1 scope.

### Binary healthy/unhealthy status
Rejected because missing evidence and stale evidence must not be treated as failure or success. `UNKNOWN` is required.

## Future compatibility

Future subsystems may publish read-only observations into the same Guardian pipeline without changing the reducer contract. Robot integration, motion controls, E-stop, autonomy, and state-engine work are intentionally deferred and require separate design decisions.

## Phase 1 requiredness lock (2026-09-23)

Phase 1 owner lock: Pi/Jetson agents, DHRAS vision+dashboard+MCP, TV MCP, and backyard/indoor cameras are required. Frontyard camera and health/fitness/git/tunnel/portal services are optional. Public paths and auth probes are not required until dedicated Access identity / auth probe exist. See `docs/VERIFY_LIVE.md` status update.

