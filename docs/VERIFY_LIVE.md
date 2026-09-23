# VERIFY_LIVE Inventory

Machine-local inventory was completed on 2026-09-23 for the Pi and Jetson. The following are now verified live:

- Pi hostname/IP: `RasPi` / `192.168.1.18`
- Jetson hostname/IP: `Jetson` / `192.168.1.17`
- Pi metric sources: `uptime`, `free`, `df`, `vcgencmd measure_temp`
- Jetson metric sources: `uptime`, `free`, `df`, `tegrastats`
- DHRAS dashboard: `vision-dashboard.service`, `127.0.0.1:8080`, `/api/cameras/status`
- DHRAS vision process: `vision_service.py`, `0.0.0.0:8081`, `/health`, `/status/<camera>`
- DHRAS MCP: `dhras-mcp.service`, `127.0.0.1:8020`
- Fitbit MCP runtime host: Pi
- Fitbit unit: `fitbit-mcp.service`
- Fitbit local endpoint: `127.0.0.1:8010/mcp`
- Fitbit tunnel unit: `cloudflared-fitbit-mcp.service`
- TV MCP runtime host: Pi
- TV MCP unit: `tv-mcp.service`
- TV MCP local listener: `127.0.0.1:8065`, MCP path `/mcp`
- TV tunnel unit: `cloudflared-tv-mcp.service`
- Pi Git Audit process: `/home/KA_PI/git-audit-mcp/git-audit-mcp/server.py`, listener `127.0.0.1:8000`
- Pi Git tunnel unit: `cloudflared-pi-git-mcp.service`
- Polar H10 MCP process: `/home/KA_PI/polar-h10/mcp_server.py`, listener `127.0.0.1:8030`
- Polar H10 tunnel unit: `cloudflared-polar-h10-mcp.service`
- Jetson Git Audit process: `/home/KA_JET/git-audit-mcp/git-audit-mcp/server.py`, listener `127.0.0.1:8000`
- Jetson Git tunnel unit: `cloudflared-jetson-git-mcp.service`

Still `VERIFY_LIVE` before Guardian treats these as authoritative:

- Pi local-agent endpoint and implementation
- Jetson local-agent endpoint and implementation
- DHRAS vision startup ownership: `vision-hub.service` is enabled but was inactive while `vision_service.py` was live; determine whether startup is manual/`start.sh`/another supervisor
- DHRAS MCP MCP path, public URL, tool count, and safe read-only probe tool
- Fitbit public route health, live tool count, and safe read-only probe tool
- TV public URL, live tool count, and safe read-only probe tool
- Pi Git Audit systemd unit, MCP path, public URL, live tool count, and safe read-only probe tool
- Polar H10 systemd unit, MCP path, public URL, live tool count, and safe read-only probe tool
- Jetson Git Audit systemd unit, MCP path, public URL, live tool count, and safe read-only probe tool
- Cloudflare tunnel routes/hostnames for all services
- unified MCP portal URL and catalog behavior
- dedicated Guardian auth probe identity and status method

## Live anomaly captured during inventory

The DHRAS vision service was running, but `frontyard` reported `online: false` while `backyard` and `indoor` reported `online: true`. Mission Control must preserve per-camera health and must not collapse the entire DHRAS vision service to GREEN solely because port `8081` is listening.

Until verified, unknown fields remain `VERIFY_LIVE` and must not be replaced with remembered values or guesses.
