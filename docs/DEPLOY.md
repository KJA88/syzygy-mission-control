# Deployment

Guardian, Mission Control, and the Mission Engine run from this repository on the SYZYGY Pi/Jetson pair. Python 3.10+ and `python3-venv` are required. Run installation as the normal login user; the installer uses `/usr/bin/python3` so an activated Jetson vision environment cannot become Guardian's base interpreter.

Current topology and ownership are in [ARCHITECTURE_CURRENT.md](ARCHITECTURE_CURRENT.md). Verify the live checkout, unit files, and ports before any restart. Subsystem repos and runtime directories are not all this git tree.

## Live topology

- Pi `RasPi` — `192.168.1.18`
  - local agent: `http://192.168.1.18:9071/health`
  - Guardian aggregator
  - Mission Control UI: `http://192.168.1.18:9070/`
- Jetson `Jetson` — `192.168.1.17`
  - local agent: `http://192.168.1.17:9071/health`
- DHRAS production vision owner: `vision-hub.service` on the Jetson
- RoArm command execution is a separate control service. Its transport stays in the RoArm repository. Mission Control reaches it through named skills.
- Home Assistant Container files: `/home/KA_PI/syzygy-runtime/home-assistant/`
- Home Assistant environment file: `/home/KA_PI/syzygy-runtime/home-assistant.env` (mode 0600, never commit or print it)
- Home Assistant listens on port 8123 on the Pi. Do not publish it.
- Home Assistant MCP: `127.0.0.1:8095` only (`syzygy-home-assistant-mcp.service`)

Also live on the Pi as of 2026-10-05, and not part of the Mission Control hostname:

- Health Workbook Service `127.0.0.1:5052` (`syzygy-health-workbook.service`)
- Withings callback `127.0.0.1:8791` (`python -m health_workbook.withings_callback`), public `https://withings.syzygylab.net/callback`
- SYZYGY Registry `127.0.0.1:8076` (`syzygy-registry.service`), public `https://registry.syzygylab.net/mcp`
- Polar Wi-Fi helper socket `/run/syzygy-polar-wifi/control.sock` (`syzygy-polar-wifi.service`)
- `syzygy-data.service` on `127.0.0.1:8765` from `/home/KA_PI/syzygy-data`, which is not the Health Workbook Service

Do not publish 5052. The portal is `https://mcp.syzygylab.net/mcp`. Details and limits are in [ARCHITECTURE_CURRENT.md](ARCHITECTURE_CURRENT.md).

All runtime service definitions, requiredness, endpoints, intervals, and verified MCP catalogs come from `config/services.yaml`.

## Install / refresh Jetson

```bash
cd "$HOME"
test -d syzygy-mission-control/.git || git clone https://github.com/KJA88/syzygy-mission-control.git
cd syzygy-mission-control
git pull --ff-only
sudo apt-get update
sudo apt-get install -y python3-venv
bash scripts/install.sh jetson
curl --fail --retry 15 --retry-connrefused --retry-delay 2 --max-time 5 http://192.168.1.17:9071/health
```

## Install / refresh Pi

```bash
cd "$HOME"
test -d syzygy-mission-control/.git || git clone https://github.com/KJA88/syzygy-mission-control.git
cd syzygy-mission-control
git pull --ff-only
sudo apt-get update
sudo apt-get install -y python3-venv
bash scripts/install.sh pi
curl --fail --retry 15 --retry-connrefused --retry-delay 2 --max-time 5 http://192.168.1.18:9071/health
curl --fail --retry 15 --retry-connrefused --retry-delay 2 --max-time 5 http://192.168.1.17:9071/health
sudo systemctl status syzygy-agent.service syzygy-guardian.service syzygy-mission-control.service --no-pager
.venv/bin/python -m guardian.inspect
```

The Pi installer installs/restarts the Pi agent, Guardian, and Mission Control systemd units. The Jetson installer installs/restarts only the Jetson agent. Agents expose cached read-only `/health` JSON on the LAN and provide no command-execution API.

Do not publish ports `9070` or `9071` directly, and do not put Home Assistant, RoArm, Vision Hub, MQTT, or MCP ports on the Mission Control hostname. Remote Mission Control is only `https://mission.syzygylab.net` via Cloudflare Access and the Pi `cloudflared.service` tunnel to `http://127.0.0.1:9070`. That unit is separate from the `cloudflared-*-mcp.service` connectors.

On the Pi, `scripts/install.sh` installs the checked-in `systemd/syzygy-mission-control.service`, including its Home Assistant `EnvironmentFile`. It does not write a second Mission Control unit. The unit description text may still say "V0.1 LAN read-only"; that string is not the capability boundary.

## Phase 1 health policy

Required for overall GREEN:

- nodes: `pi`, `jetson`
- services: `dhras-vision-service`, `dhras-dashboard`, `dhras-mcp`, `tv-mcp`
- cameras: `backyard`, `indoor`

Optional and nonblocking when hard-failed/unknown:

- `frontyard` camera
- `fitbit-mcp`, `polar-h10-mcp`, `pi-git-mcp`, `jetson-git-mcp`
- Cloudflare path objects and unified portal
- public MCP layers while `public_required: false`
- auth while the dedicated Guardian auth probe is disabled

Optional hard failures remain visible in the snapshot/UI but do not degrade their parent. Optional soft integrity warnings such as catalog drift or unresolved conflicts remain YELLOW.

## Routine probe behavior

Routine MCP health traffic is limited to:

1. `initialize`
2. `notifications/initialized`
3. `tools/list`

Guardian does not call application tools for routine health checks. Catalog drift is visible as YELLOW. The dedicated probe is the Cloudflare Access identity in [AUTH_PROBE.md](AUTH_PROBE.md). With no identity file, auth stays UNKNOWN. With the identity configured, the probe still stops at `initialize`, `notifications/initialized`, and `tools/list`.

DHRAS camera state is preserved per camera. `frontyard` is currently optional; `backyard` and `indoor` are required. Production DHRAS startup is systemd-only through `vision-hub.service`; do not use the legacy `robotics/jetson-vision/start.sh` path in production.

## Metrics

Agents report CPU load, available RAM, free disk, and readable thermal sensors. Jetson GPU utilization uses `/sys/devices/platform/bus@0/17000000.gpu/load` first and `tegrastats` `GR3D_FREQ` as fallback. Missing metrics remain null; Guardian does not invent values.

## Mission Control UI

LAN origin, also reached remotely through Access (see [REMOTE_ACCESS.md](REMOTE_ACCESS.md)):

```text
http://192.168.1.18:9070/
https://mission.syzygylab.net
```

To install/enable the UI unit explicitly:

```bash
cd ~/syzygy-mission-control
sudo cp systemd/syzygy-mission-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now syzygy-mission-control.service
curl -sS http://127.0.0.1:9070/api/health
```

## Manual one-shot checks

Use a separate state directory while the systemd Guardian owns `state/`:

```bash
cd ~/syzygy-mission-control
.venv/bin/python -m agents.local --node pi --once --output state/manual-agent.json
.venv/bin/python -m guardian.aggregator --once --state-dir state/manual
.venv/bin/python -m guardian.inspect --snapshot state/manual/snapshot.json
```

On Jetson substitute `--node jetson` for the agent command.

`state/snapshot.json` is written atomically with temp file + fsync + replace. Events append to `state/events.jsonl` and are fsynced. Every consumer must enforce the configured Guardian heartbeat freshness guard; stale snapshots never remain GREEN indefinitely.

## Tests and live acceptance

Run:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Live Pi acceptance on 2026-09-23 passed **31 tests** and produced:

```text
SYSTEM: green
VISION: green required=True
frontyard red required=False
backyard green required=True
indoor green required=True
```

At that check `syzygy-agent.service`, `syzygy-guardian.service`, and `syzygy-mission-control.service` were all active.

## Logs

```bash
# Either host
journalctl -u syzygy-agent -n 50 --no-pager

# Pi
journalctl -u syzygy-guardian -n 50 --no-pager
journalctl -u syzygy-mission-control -n 50 --no-pager
```

## Cloudflare APT signing-key recovery

If `apt-get update` fails with Cloudflare `NO_PUBKEY`, refresh the official scoped keyring and retry prerequisites:

```bash
(
set -euo pipefail
keyfile="$(mktemp)"
trap 'rm -f "$keyfile"' EXIT
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg -o "$keyfile"
test -s "$keyfile"
sudo install -d -m 0755 /usr/share/keyrings
sudo install -m 0644 "$keyfile" /usr/share/keyrings/cloudflare-main.gpg
printf '%s\n' 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' |
  sudo tee /etc/apt/sources.list.d/cloudflared.list >/dev/null
sudo apt-get update
sudo apt-get install -y python3-venv
)
```

This refreshes package metadata only; it does not restart or upgrade cloudflared.

## Adding cameras

See [CAMERAS.md](CAMERAS.md) for C210/C500 private Vision Hub setup and acceptance.
Both new cameras use existing generic health checks and are optional.
