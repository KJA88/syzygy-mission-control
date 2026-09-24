# Mission Control V0.1 (LAN, read-only)

Mission Control is the read-only SYZYGY cockpit served from the Pi at:

```text
http://192.168.1.18:9070/
```

The browser renders Guardian outputs only. It never probes MCP, hosts, Cloudflare, or other services directly. The UI server reads:

- `state/snapshot.json`
- `state/events.jsonl`

RoArm, motion, E-stop, and recovery controls are out of scope for V0.1.

## Architecture

```text
Pi/Jetson local agents
        |
        v
Guardian aggregator
        |
        +--> state/snapshot.json
        +--> state/events.jsonl
                    |
                    v
          ui/server.py :9070
                    |
                    v
             browser renderer
```

Statuses are GREEN / YELLOW / RED / UNKNOWN.

## Heartbeat freshness

A saved snapshot cannot update itself after Guardian stops. Every consumer must apply the hard-stale guard. If `guardian.heartbeat_at` is missing or older than the configured threshold (default 120 seconds):

- `system.status` becomes `unknown`
- `system.reason` becomes `GUARDIAN_HEARTBEAT_STALE`
- `guardian.status` becomes `unknown`
- `guardian.class` becomes `SNAPSHOT_STALE`

## Phase 1 health policy

Required for overall GREEN:

- Pi node
- Jetson node
- DHRAS vision service
- DHRAS dashboard
- DHRAS MCP
- TV MCP
- backyard camera
- indoor camera

Optional hard failures remain visible but do not block GREEN. This includes the parked `frontyard` camera plus Fitbit, Polar H10, Git Audit MCPs, non-required public paths, portal state, and auth while the dedicated auth probe is off.

Optional soft integrity warnings such as catalog drift or unresolved conflicts still make the affected parent YELLOW.

## Current live acceptance

On 2026-09-23 the Pi passed 31 tests and reported:

```text
SYSTEM: green
VISION: green required=True
frontyard red required=False
backyard green required=True
indoor green required=True
```

At that check the Pi agent, Guardian, and Mission Control systemd services were all active.

## systemd on Pi

Mission Control should run under systemd so it survives reboot:

```bash
cd ~/syzygy-mission-control
sudo cp systemd/syzygy-mission-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now syzygy-mission-control.service
curl -sS http://127.0.0.1:9070/api/health
```

A fresh `bash scripts/install.sh pi` also installs/enables the Mission Control unit.

Do not publish port `9070` through Cloudflare or public DNS.

## Manual run

For troubleshooting only:

```bash
cd ~/syzygy-mission-control
bash scripts/run-mission-control.sh
```

Or explicitly:

```bash
python3 ui/server.py --host 0.0.0.0 --port 9070 --state-dir state --ui-dir ui
```

## Auto-refresh

The browser polls `/api/snapshot` and `/api/events?limit=20` about every 7 seconds.

## Offline fixture check

```bash
cd ~/syzygy-mission-control
mkdir -p /tmp/mc-fixture-state
cp tests/fixtures/snapshot.json /tmp/mc-fixture-state/snapshot.json
cp tests/fixtures/events.jsonl /tmp/mc-fixture-state/events.jsonl
python3 ui/server.py --host 127.0.0.1 --port 9070 --state-dir /tmp/mc-fixture-state --ui-dir ui
```

Then query:

```bash
curl -sS http://127.0.0.1:9070/api/snapshot | python3 -m json.tool
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## Out of scope for V0.1

- RoArm / joints / motion / E-stop
- mutation or recovery buttons
- direct browser-to-agent probing
- direct browser-to-MCP probing
- public exposure of the Mission Control UI
