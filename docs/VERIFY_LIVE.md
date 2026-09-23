# VERIFY_LIVE Inventory

These values must be confirmed from the machines before Guardian implementation treats them as authoritative.

- Pi LAN IP / hostname
- Jetson still at `192.168.1.17`
- Pi local-agent endpoint
- Jetson local-agent endpoint
- DHRAS live startup method (`vision-dashboard.service` vs `./start.sh`; whether per-camera units are active or obsolete)
- Fitbit runtime host
- Fitbit systemd unit
- Fitbit MCP path (`/` vs `/mcp`)
- Fitbit public full route
- Fitbit live tool count from `tools/list`
- safe read-only Fitbit probe tool
- tv-mcp host, systemd unit, local URL/port, public URL, live tool count, safe probe tool
- pi-git MCP systemd unit, local URL/port, public URL, live tool count, safe probe tool
- jetson-git MCP systemd unit, local URL/port, public URL, live tool count, safe probe tool
- cloudflared units, tunnel IDs, and routes
- unified MCP portal URL and catalog behavior
- dedicated Guardian auth probe identity and status method
- current CPU/RAM/disk/temp metric sources on both hosts

Until verified, unknown fields remain `VERIFY_LIVE` and must not be replaced with remembered values or guesses.
