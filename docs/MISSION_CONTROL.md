# Mission Control V0.1 (LAN, read-only)

Dark cockpit UI that **renders Guardian outputs only**. The browser never probes
MCP, hosts, Cloudflare, or any service. The tiny UI server only reads local files:

- `state/snapshot.json`
- `state/events.jsonl`

RoArm is out of scope. There are no mutation / recovery buttons in V0.1.

## Architecture

```
[Guardian aggregator] --> state/snapshot.json + state/events.jsonl
                                    |
                                    v
                      [ui/server.py  :9070  LAN-only]
                                    |
                                    v
                         [Browser renderer]
```

Statuses shown as **GREEN / YELLOW / RED / UNKNOWN**.

### Heartbeat freshness (mandatory)

A saved snapshot cannot update itself after Guardian dies. Every consumer must
apply the hard-stale guard. This UI server mirrors `guardian.storage.read_snapshot`:

- If `guardian.heartbeat_at` is missing or older than **120 seconds**, then:
  - `system.status` → `unknown`
  - `system.reason` → `GUARDIAN_HEARTBEAT_STALE`
  - `guardian.status` → `unknown`
  - `guardian.class` → `SNAPSHOT_STALE`

Threshold is configurable (`--hard-stale-s` / `MC_HARD_STALE_S`), default 120.

### Optional services vs system GREEN

Current `config/services.yaml` marks nodes/services/paths as **optional**.
System GREEN therefore does **not** mean every service is healthy. The UI always
shows system status **and** per-service / per-node cards. Read the cards.

Auth probes are off in V0.1 → auth cards stay honest **UNKNOWN** (`AUTH_PROBE_OFF`).

## Run on Pi (next to Guardian)

From `~/syzygy-mission-control` after the UI files are present:

```bash
# Preferred convenience launcher (binds 0.0.0.0:9070, state=./state)
bash scripts/run-mission-control.sh
```

Or explicitly:

```bash
# LAN bind so TV/other LAN browsers can open it
python3 ui/server.py --host 0.0.0.0 --port 9070 --state-dir state --ui-dir ui

# Or module entry (same server):
python3 -m guardian.ui_server --host 0.0.0.0 --port 9070 --state-dir state --ui-dir ui
```

Open: `http://192.168.1.18:9070/`

Guardian must already be writing `state/snapshot.json`. If the systemd Guardian
owns `state/`, point Mission Control at the same directory (default). For a
manual one-shot Guardian check directory, pass that path instead:

```bash
MC_STATE_DIR=$HOME/syzygy-mission-control/state/manual \
  bash scripts/run-mission-control.sh
```

## LAN-only (never Cloudflare)

Mission Control is an unauthenticated local renderer. Bind to LAN
(`127.0.0.1` or `0.0.0.0` / `192.168.1.18`) and **do not** publish port 9070
through Cloudflare, public DNS, or any tunnel.

## Verify without live Guardian

```bash
cd /path/to/syzygy-mission-control   # or this package root
mkdir -p /tmp/mc-fixture-state
cp tests/fixtures/snapshot.json /tmp/mc-fixture-state/snapshot.json
cp tests/fixtures/events.jsonl /tmp/mc-fixture-state/events.jsonl
python3 ui/server.py --host 127.0.0.1 --port 9070 --state-dir /tmp/mc-fixture-state --ui-dir ui
# curl http://127.0.0.1:9070/api/snapshot | jq .system,.guardian
# open http://127.0.0.1:9070/
```

Unit tests:

```bash
python3 -m unittest discover -s tests -v
```

## Auto-refresh

The browser polls `/api/snapshot` and `/api/events?limit=20` about every 7 seconds.

## Out of scope (V0.1)

- RoArm / joints / E-stop / motion
- Mutation or recovery buttons
- Agent health endpoints called from the browser
- Any network probe from UI or UI server except reading local files

## systemd on Pi (survives reboot)

Prefer the unit over a manual  launcher. After files are on disk:

 152640

Fresh  also installs/enables this unit when the UI launcher exists.
Do not publish port 9070 via Cloudflare.

## systemd on Pi (survives reboot)

Prefer the unit over a manual background launcher. After files are on disk:

```bash
cd ~/syzygy-mission-control
fuser -k 9070/tcp 2>/dev/null || true
sudo cp systemd/syzygy-mission-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now syzygy-mission-control.service
curl -sS http://127.0.0.1:9070/api/health
```

Fresh `bash scripts/install.sh pi` also installs/enables this unit when the UI launcher exists.
Do not publish port 9070 via Cloudflare.
