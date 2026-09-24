# VERIFY_LIVE Inventory

Machine-local and public MCP inventory was completed on 2026-09-23 for the Pi and Jetson.

## Verified live

- Pi hostname/IP: `RasPi` / `192.168.1.18`
- Jetson hostname/IP: `Jetson` / `192.168.1.17`
- Pi metric sources: `uptime`, `free`, `df`, `vcgencmd measure_temp`
- Jetson metric sources: `uptime`, `free`, `df`, `tegrastats`
- DHRAS dashboard: `vision-dashboard.service`, `127.0.0.1:8080`, `/api/cameras/status`
- DHRAS vision process: `vision_service.py`, `0.0.0.0:8081`, `/health`, `/status/<camera>`
- DHRAS MCP: `dhras-mcp.service`, local `127.0.0.1:8020/mcp`, public `https://dhras.syzygylab.net/mcp`, 16 tools
- Fitbit MCP: `fitbit-mcp.service`, local `127.0.0.1:8010/mcp`, public `https://fitbit.syzygylab.net/mcp`, 62 tools
- TV MCP: `tv-mcp.service`, local `127.0.0.1:8065/mcp`, public `https://tv.syzygylab.net/mcp`, 7 tools, server `tv-mcp` v1.30.0
- Pi Git Audit MCP: `git-audit-mcp.service`, local `127.0.0.1:8000/mcp`, public `https://pi-git.syzygylab.net/mcp`, 22 tools
- Polar H10 MCP: `polar-h10-mcp.service`, local `127.0.0.1:8030/mcp`, public `https://polar.syzygylab.net/mcp`, 4 tools
- Jetson Git Audit MCP: `git-audit-mcp.service`, local `127.0.0.1:8000/mcp`, public `https://jetson-git.syzygylab.net/mcp`, 22 tools
- Pi tunnel units: `cloudflared-fitbit-mcp.service`, `cloudflared-pi-git-mcp.service`, `cloudflared-polar-h10-mcp.service`, `cloudflared-tv-mcp.service`
- Jetson tunnel units: `cloudflared-jetson-git-mcp.service`, `cloudflared.service`
- Local and public MCP `initialize` + `tools/list` succeed for all in-scope MCP services above.
- Git Audit, DHRAS, and TV use MCP session IDs; Fitbit and Polar currently do not require a session header for the tested flow.

## Important negative findings

Generated-name guesses such as `*-mcp.syzygylab.net` were not valid public routes during the batch test. Guardian must use only the verified public hostnames above and must not infer public routes from service names.

RoArm was discovered locally/publicly during inventory but remains explicitly out of Mission Control V0.1 scope and is not added to service configuration.

## Still VERIFY_LIVE / design work remaining

- Pi local-agent implementation complete; deploy and verify candidate `http://192.168.1.18:9071/health`
- Jetson local-agent implementation complete; deploy and verify candidate `http://192.168.1.17:9071/health`
- Verify LAN bind/port availability, systemd permissions, agent JSON freshness and restart recovery on both hosts
- Verify one Guardian tick on Pi against both agents and all six public routes; compare per-camera output with DHRAS endpoints
- [done 2026-09-23] Verify GPU utilization adapter on Jetson — sysfs load millipercent + tegrastats fallback; alert thresholds still TBD
- [done 2026-09-23] DHRAS vision startup ownership: intended owner is `vision-hub.service`. Orphan manual/`start.sh` process stopped; unit restarted and verified Active under systemd; Guardian local_service green. Production startup = systemd only (do not use `start.sh`). Frontyard offline remains parked coupler.
- DHRAS dashboard/vision public URLs if they are intended to be public separately from DHRAS MCP
- unified MCP portal URL and catalog behavior
- dedicated Guardian auth probe identity and status method
- final required/optional policy for nodes and services before production reduction

## Live anomaly captured during inventory

The DHRAS vision service was running, but `frontyard` reported `online: false` while `backyard` and `indoor` reported `online: true`. Mission Control must preserve per-camera health and must not collapse the entire DHRAS vision service to GREEN solely because port `8081` is listening.

Unknown fields remain `VERIFY_LIVE`; do not replace them with guessed or generated values.

## Backend implementation validation

The repository now includes local agents, protocol-only MCP probes, the reducer,
atomic snapshot writes, append-only health events, deterministic fixture tests,
and a systemd installer. See [DEPLOY.md](DEPLOY.md). Hardware deployment and new
endpoint verification have not been performed by the implementation test suite.
Candidate agent URLs are opt-in until verified; existing required flags are unchanged.

## Status update 2026-09-23 (Mission Control V0.1)

Resolved / live now:

- Pi agent `http://192.168.1.18:9071/health` and Jetson agent `http://192.168.1.17:9071/health` deployed; Guardian ticks on Pi
- Mission Control UI on Pi: `http://192.168.1.18:9070/` (commit `8fd10cf`+); renderer-only against `state/snapshot.json`
- Thermal metrics hotfix on Jetson (`6bde31e`); agents + guardian green in live snapshot

Still open (unchanged intent):

- [done 2026-09-23] Promote agents off `--use-candidate-agents` — `agent` URLs set to live `/health` endpoints; flag removed from installer
- [done 2026-09-23] Jetson GPU utilization adapter — sysfs `17000000.gpu/load` (+ tegrastats GR3D_FREQ fallback)
- [done 2026-09-23] DHRAS vision startup ownership restored under `vision-hub.service` (orphan/`start.sh` path retired for production)
- Dedicated Guardian auth probe identity (auth stays `AUTH_PROBE_OFF` / UNKNOWN)
- Final required vs optional policy before production reduction
- Public MCP HTTP 403 without dedicated Access identity is expected; local probes remain trusted path
- Frontyard camera offline until Ethernet coupler

## Status update 2026-09-23 (agent URL promotion)

- Pi agent verified: `http://192.168.1.18:9071/health` → written to `nodes.pi.agent`
- Jetson agent verified: `http://192.168.1.17:9071/health` → written to `nodes.jetson.agent`
- `scripts/install.sh` Guardian unit no longer passes `--use-candidate-agents`
- Live Pi unit still needs one `sudo` rewrite/restart to drop the flag (owner paste)

## Status update 2026-09-23 (Jetson GPU adapter)

- Live evidence: `/sys/devices/platform/bus@0/17000000.gpu/load` world-readable; `tegrastats` shows `GR3D_FREQ N%`
- `nvidia-smi --query-gpu=utilization.gpu` returns `N/A` on this board — not used
- Agent `metrics.gpu_utilization_percent` reads sysfs first (0-1000 → %), else tegrastats; remains null if neither works (e.g. Pi)
- Alert thresholds for GPU still TBD (not set in this change)

## Status update 2026-09-23 (DHRAS vision-hub ownership)

- Finding: `vision-hub.service` enabled but inactive since 2026-09-20; live process was orphan under PID 1 from 2026-09-22 (`start.sh`/manual), while `vision-dashboard.service` and `dhras-mcp.service` stayed correctly under systemd.
- Fix (owner-approved): `pkill` orphan `vision_service.py`, then `sudo systemctl start vision-hub.service`.
- Verified: unit Active/running in `/system.slice/vision-hub.service`; health `http://192.168.1.17:8081/health` -> backyard/indoor true, frontyard false; agent layers `local_service`/`local_process`/`local_port`/`local_health` green.
- Rule: production startup = systemd only. Repo `start.sh` is legacy dual-start and can recreate the orphan mismatch.
