# Guardian V0.1 deployment

Python 3.10+ and python3-venv are required. Run as the normal Pi/Jetson login user.
The installer uses `/usr/bin/python3` so an activated Jetson vision environment
does not become Guardian's base interpreter.
Install the Jetson first, then Pi. Guardian runs on Pi. All configuration is in
`config/services.yaml`; no unit names, service URLs, or tool catalogs are discovered or guessed.

## Jetson

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

## Pi

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
sudo systemctl status syzygy-agent.service syzygy-guardian.service --no-pager
.venv/bin/python -m guardian.inspect
```

The scripts install/restart only SYZYGY's new units. Agent port 9071 is a proposed
deployment setting, not a previously verified live fact. Confirm it is free before
installation (`ss -ltn 'sport = :9071'`). Agents bind only the configured LAN address,
serve only cached `/health` JSON, and offer no command execution or control API.
Restrict LAN access to trusted hosts with your existing firewall; do not publish these
unauthenticated agent endpoints through Cloudflare. No firewall or tunnel is changed.

The installer opts Guardian into `agent_candidate_url` using `--use-candidate-agents`.
After both endpoints are verified, set each node's `agent` to its verified URL and remove
the opt-in flag from the Guardian unit. Keep live-verification dates honest.

## If APT stops on Cloudflare signing keys

The 2026-09-23 deployment logs from both hosts stopped at `apt-get update` with
`NO_PUBKEY` / missing signing keys. The Guardian installer had not run at that point.
Cloudflare documents its signing-key rollover and current scoped keyring at
[the official package repository](https://pkg.cloudflare.com/index.html).

Refresh the official key and standard cloudflared source entry, then retry prerequisites:

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

This refreshes package metadata and installs the Python prerequisite; it does not
upgrade or restart cloudflared. Keep signature verification enabled. If APT reports
duplicate/conflicting source entries, inspect those entries before changing them.
Then run the appropriate `bash scripts/install.sh jetson` or `bash scripts/install.sh pi`
from the updated checkout. Agent startup completes an initial probe before binding;
the retrying health requests above allow for that startup time.

## Foreground / one-shot operation

```bash
# On each respective host; substitute jetson on Jetson:
.venv/bin/python -m agents.local --node pi --once --output state/agent.json
.venv/bin/python -m agents.local --node pi
# On Pi (agents must already be running):
.venv/bin/python -m guardian.aggregator --use-candidate-agents --once --state-dir state/manual
.venv/bin/python -m guardian.inspect --snapshot state/manual/snapshot.json
```

Only one Guardian writer may own a state directory. Use a separate directory for
manual checks while the systemd service runs. `snapshot.json` is replaced using a
same-directory temporary file, file fsync, atomic replace, and directory fsync on Linux.
Events append before the snapshot, are fsynced, and include trace IDs. A crash between
event append and snapshot replacement can replay a transition under a new trace ID;
consumers must tolerate such replay. Event rotation is an operator responsibility in this skeleton.

## Health semantics and operational limits

Wire statuses are lowercase per schema v1: GREEN/YELLOW/RED/UNKNOWN map to
`green/yellow/red/unknown`. Existing required flags are preserved: currently every
node/service/path is optional. Thus system GREEN does not mean every optional service
is healthy. Inspect individual objects; approve final requiredness before production.

Routine MCP traffic is initialize, notifications/initialized, and tools/list only.
No functional tools are invoked even if an allowed/optional tool appears in config.
The MCP probe validates IDs, errors, server identity, names/count fingerprints, sessions,
and pagination. Catalog drift is YELLOW. Auth and portal layers stay UNKNOWN until
their dedicated implementations and live verification exist.

Per-camera online values are retained as their own required layers within the vision
service. The non-authoritative vision-hub unit can degrade to YELLOW, but cannot alone
make a working vision process RED. An offline required camera makes vision RED and its
dependents inherit the classified dependency failure. This does not alter the existing
optional status of the service at system level.

The agent reports CPU load, available RAM, free disk, and thermal sensors when readable.
GPU utilization is explicitly null pending the Jetson metric adapter. No missing metric
is fabricated. Auth sessions, tool invocation history, and operator activity ingestion
are not implemented; activity currently contains health status transitions only.

A saved file cannot update itself after Guardian dies. Every consumer must apply the
120-second heartbeat guard when reading it; `guardian.inspect` / `read_snapshot` do so.
Freshness thresholds and polling intervals are configuration-driven. Cached observations
retain original timestamps and trace IDs, rather than being relabeled as fresh each tick.

## Tests

Implementation validation on the development workstation: 16 unittest methods pass,
including all 15 locked reducer fixtures and real loopback JSON/SSE protocol tests.
A one-shot aggregator run wrote snapshot/events successfully. All six external MCP
routes returned HTTP 403 from that workstation; this does not reverify Pi/Jetson
connectivity or supersede the original live inventory. Repeat the tick on Pi after deployment.

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Tests use deterministic fixtures and local mock HTTP servers, not Pi/Jetson hardware.
For service logs: `journalctl -u syzygy-agent -n 50 --no-pager` on either host and
`journalctl -u syzygy-guardian -n 50 --no-pager` on Pi.

## Mission Control UI (Pi only)

LAN read-only UI on port 9070. After Guardian is up:

Already up to date.

Open  on LAN only. Do not publish  through Cloudflare.
A fresh  writes/enables this unit when  is present.

## Mission Control UI (Pi only)

LAN read-only UI on port 9070. After Guardian is up:

```bash
cd ~/syzygy-mission-control
git pull --ff-only
fuser -k 9070/tcp 2>/dev/null || true
sudo cp systemd/syzygy-mission-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now syzygy-mission-control.service
curl --fail --retry 10 --retry-connrefused --retry-delay 1 --max-time 5 http://127.0.0.1:9070/api/health
```

Open `http://192.168.1.18:9070/` on LAN only. Do not publish `:9070` through Cloudflare.
A fresh `bash scripts/install.sh pi` writes/enables this unit when `scripts/run-mission-control.sh` is present.
