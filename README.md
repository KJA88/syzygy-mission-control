# SYZYGY Mission Control

Central observability and operations cockpit for the SYZYGY infrastructure.

## Current scope

V0.1 covers infrastructure only:

- Raspberry Pi host health
- Jetson host health
- MCP services and tool catalogs
- Cloudflare/public paths
- authentication probe state
- CPU/GPU/RAM/disk/temperature metrics
- last tool invocation and failed calls
- DHRAS / Vision Hub
- TV MCP
- Fitbit MCP
- operator/activity events

**RoArm is intentionally out of scope for V0.1.** No robot state, joint state, motion, or E-stop logic belongs in this phase.

## Architecture

Pi local agent + Jetson local agent -> Guardian aggregator -> versioned snapshot + append-only events -> Mission Control UI.

The browser is a renderer only. It never probes infrastructure directly.

## Status model

`GREEN` / `YELLOW` / `RED` / `UNKNOWN`

Overall health is reduced from components marked `required: true`. Missing or stale evidence blocks GREEN.

## Run the backend

See [deployment instructions](docs/DEPLOY.md) for Pi/Jetson installation, foreground
commands, tests, freshness handling, and the remaining live verification boundaries.

The backend is implemented in `agents/` and `guardian/`. Tests are hardware-independent.

## Evidence policy

Repository-proven facts may be seeded into configuration. Anything that requires live verification remains marked `VERIFY_LIVE`. Do not replace verification markers with guesses.

This repository was initialized from the Phase 1 planning handoff produced during the 2026-09-23 SYZYGY Mission Control design session. Some files are reconstructed from that handoff rather than copied byte-for-byte from Grok's private workspace.
