# VERIFY_LIVE Inventory

Machine-local and public MCP inventory was completed on 2026-09-23 for the Pi and Jetson. Mission Control V0.1 is now deployed and live on the LAN.

## Verified live

### Hosts and agents

- Pi: `RasPi` / `192.168.1.18`
- Jetson: `Jetson` / `192.168.1.17`
- Pi agent: `http://192.168.1.18:9071/health`
- Jetson agent: `http://192.168.1.17:9071/health`
- Guardian aggregator: Pi
- Mission Control UI: `http://192.168.1.18:9070/`
- Pi `syzygy-agent.service`, `syzygy-guardian.service`, and `syzygy-mission-control.service` were active in the latest live acceptance check.

### DHRAS

- dashboard: `vision-dashboard.service`, `127.0.0.1:8080`, `/api/cameras/status`
- vision: `vision-hub.service` is the authoritative production owner; `vision_service.py` listens on `0.0.0.0:8081`
- health: `/health`, per-camera `/status/<camera>`
- DHRAS MCP: `dhras-mcp.service`, local `127.0.0.1:8020/mcp`, public `https://dhras.syzygylab.net/mcp`, 16 tools
- production rule: do not use legacy `robotics/jetson-vision/start.sh`

Latest accepted camera state:

- `frontyard`: red/offline, optional
- `backyard`: green, required
- `indoor`: green, required

### Other MCP services

- Fitbit MCP: `fitbit-mcp.service`, local `127.0.0.1:8010/mcp`, public `https://fitbit.syzygylab.net/mcp`, 62 tools
- TV MCP: `tv-mcp.service`, local `127.0.0.1:8065/mcp`, public `https://tv.syzygylab.net/mcp`, 7 tools, server `tv-mcp` v1.30.0
- Pi Git Audit MCP: `git-audit-mcp.service`, local `127.0.0.1:8000/mcp`, public `https://pi-git.syzygylab.net/mcp`, 22 tools
- Polar H10 MCP: `polar-h10-mcp.service`, local `127.0.0.1:8030/mcp`, public `https://polar.syzygylab.net/mcp`, 4 tools
- Jetson Git Audit MCP: `git-audit-mcp.service`, local `127.0.0.1:8000/mcp`, public `https://jetson-git.syzygylab.net/mcp`, 22 tools

Local and public MCP `initialize` + `tools/list` were verified for all in-scope MCP services. Git Audit, DHRAS, and TV use MCP session IDs; Fitbit and Polar did not require a session header in the tested flow.

### Cloudflare tunnel units

Pi:

- `cloudflared-fitbit-mcp.service`
- `cloudflared-pi-git-mcp.service`
- `cloudflared-polar-h10-mcp.service`
- `cloudflared-tv-mcp.service`

Jetson:

- `cloudflared-jetson-git-mcp.service`
- `cloudflared.service`

Generated guesses such as `*-mcp.syzygylab.net` are not authoritative. Guardian uses only the verified hostnames in `config/services.yaml`.

### Metrics

- Pi: CPU load, RAM, disk, thermal metrics
- Jetson: CPU load, RAM, disk, thermal metrics, GPU utilization
- Jetson GPU: `/sys/devices/platform/bus@0/17000000.gpu/load` with `tegrastats GR3D_FREQ` fallback
- `nvidia-smi` utilization is not used on this Jetson because it reports N/A

## Phase 1 requiredness

Required for overall GREEN:

- nodes: `pi`, `jetson`
- services: `dhras-vision-service`, `dhras-dashboard`, `dhras-mcp`, `tv-mcp`
- cameras: `backyard`, `indoor`

Optional:

- camera: `frontyard`
- `fitbit-mcp`, `polar-h10-mcp`, `pi-git-mcp`, `jetson-git-mcp`
- `cloudflare-pi`, `cloudflare-jetson`, `unified-mcp-portal`
- public MCP layers while `public_required: false`
- auth/invoke until a dedicated Guardian auth identity exists

Optional hard failures/unknown states remain visible but do not degrade the parent. Optional soft integrity warnings such as catalog drift or unresolved probe conflicts remain YELLOW.

## Latest live acceptance

On 2026-09-23 the Pi ran **31 tests** successfully and produced:

```text
SYSTEM: green
VISION: green required=True
frontyard red required=False
backyard green required=True
indoor green required=True
```

Repo head at that acceptance was `ac242e2` (`docs: clarify optional hard failures are nonblocking`).

## Remaining VERIFY_LIVE / design work

- dedicated Guardian auth probe identity and status method
- unified MCP portal URL and catalog behavior
- DHRAS dashboard/vision public URLs only if those components are intentionally published separately from DHRAS MCP
- alert thresholds for operational metrics such as Jetson GPU utilization
- physical repair/reconnection of the optional frontyard camera path

Public MCP HTTP 403 without a dedicated Cloudflare Access identity is expected from unauthorized clients and does not supersede the verified local health path.

## Scope boundary

RoArm was discovered during inventory but remains explicitly out of Mission Control V0.1 scope. Robot state, joints, motion controls, E-stop, autonomy, and robot MCP behavior are not part of this system.

Unknown fields must remain unknown/`VERIFY_LIVE`; do not replace them with guesses.
