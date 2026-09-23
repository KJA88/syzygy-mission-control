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

- Pi local-agent endpoint and implementation
- Jetson local-agent endpoint and implementation
- DHRAS vision startup ownership: `vision-hub.service` is enabled but was inactive while `vision_service.py` was live; determine whether startup is manual, `start.sh`, or another supervisor
- DHRAS dashboard/vision public URLs if they are intended to be public separately from DHRAS MCP
- unified MCP portal URL and catalog behavior
- dedicated Guardian auth probe identity and status method
- final required/optional policy for nodes and services before production reduction

## Live anomaly captured during inventory

The DHRAS vision service was running, but `frontyard` reported `online: false` while `backyard` and `indoor` reported `online: true`. Mission Control must preserve per-camera health and must not collapse the entire DHRAS vision service to GREEN solely because port `8081` is listening.

Unknown fields remain `VERIFY_LIVE`; do not replace them with guessed or generated values.
