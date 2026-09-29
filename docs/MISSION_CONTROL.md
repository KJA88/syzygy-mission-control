# Mission Control

Mission Control is the SYZYGY cockpit. It serves the UI and the HTTP API on the Pi.

```text
http://192.168.1.18:9070/
https://mission.syzygylab.net
```

The remote hostname is Cloudflare Access plus the dedicated tunnel to `http://127.0.0.1:9070`. Do not publish port 9070 on the router. See [remote access](REMOTE_ACCESS.md) and [architecture](ARCHITECTURE_CURRENT.md).

The browser renders Mission Control. It does not probe MCP servers, Home Assistant, or devices itself, and it does not hold those secrets. Live operational data comes from the network. The PWA service worker does not cache `/api/*` or control state.

## UI

Overview, Perception, Robots, Devices, Home, Missions, Events, System, and Settings.

Mission Control owns presentation and the orchestration entry points. Perception still belongs to Vision Hub. Arm commands still belong to the RoArm control service. Home writes still belong to the Home Assistant adapter and its allowlist.

## What the server reads and exposes

- Guardian snapshot and events, with the heartbeat freshness rules below
- Perception summaries from Vision Hub
- RoArm skill status through the control owner
- Home Assistant entities and the allowed On/Off actions
- Mission Engine status, start, and stop

STOP for a mission is an operational stop. It is not RoArm torque-off. Guardian does not issue those actions.

## Heartbeat freshness

A saved snapshot cannot update itself after Guardian stops. If `guardian.heartbeat_at` is missing or older than the configured threshold (default 120 seconds):

- `system.status` becomes `unknown`
- `system.reason` becomes `GUARDIAN_HEARTBEAT_STALE`
- `guardian.status` becomes `unknown`
- `guardian.class` becomes `SNAPSHOT_STALE`

## Health rollup

Required objects still decide system GREEN / YELLOW / RED / UNKNOWN. Optional hard failures stay visible and do not by themselves turn the system red. Optional soft warnings can still make a parent yellow.

Phase 1 required set:

- Pi node and Jetson node
- DHRAS vision service, dashboard, and MCP
- TV MCP
- backyard camera and indoor camera

Optional includes the parked `frontyard` camera, Fitbit, Polar H10, Git Audit MCPs, and auth while the dedicated auth probe is off. Home Assistant and a failed mission do not turn system health red.

## systemd

The installed unit is `syzygy-mission-control.service`, port 9070. The unit file in `systemd/` loads the Home Assistant environment file. `scripts/install.sh` writes a different unit that does not. Verify the installed unit before any reinstall or restart. See [deployment](DEPLOY.md).

## Tests

```bash
python -m unittest discover -s tests
node --test tests/test_ui_access.cjs
```

## Historical note

`docs/ADR-SYZ-MC-001.md` accepted a LAN read-only V0.1 cockpit and left RoArm out of scope. That decision is the 2026-09-23 baseline. Phases 2–6, Home Assistant writes, RoArm skills, missions, and the Access-protected UI are in scope now.
