#!/usr/bin/env bash
set -euo pipefail
role="${1:?Usage: bash scripts/install.sh pi|jetson}"
case "$role" in pi|jetson) ;; *) echo 'Expected pi or jetson' >&2; exit 2;; esac
root="$(cd "$(dirname "$0")/.." && pwd)"
case "$root" in *[[:space:]]*) echo 'Install from a repository path without whitespace' >&2; exit 2;; esac
account="$(id -un)"
if [[ "$account" == root ]]; then echo 'Run as your normal login user; sudo is used for unit installation only' >&2; exit 2; fi
cd "$root"
# The Jetson login may have its vision environment activated. Guardian uses
# the distro interpreter and its own environment, including under systemd.
unset PYTHONHOME PYTHONPATH VIRTUAL_ENV
/usr/bin/python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
mkdir -p state
sudo tee /etc/systemd/system/syzygy-agent.service >/dev/null <<EOF
[Unit]
Description=SYZYGY read-only $role local agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$account
WorkingDirectory=$root
ExecStart=$root/.venv/bin/python -m agents.local --node $role --output $root/state/agent.json
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=$root/state
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
if [[ "$role" == pi ]]; then
sudo tee /etc/systemd/system/syzygy-guardian.service >/dev/null <<EOF
[Unit]
Description=SYZYGY Guardian aggregator
After=network-online.target syzygy-agent.service
Wants=network-online.target syzygy-agent.service

[Service]
Type=simple
User=$account
WorkingDirectory=$root
ExecStart=$root/.venv/bin/python -m guardian.aggregator --state-dir $root/state
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=$root/state
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

sudo tee /etc/systemd/system/syzygy-mission-control.service >/dev/null <<EOF
[Unit]
Description=SYZYGY Mission Control V0.1 (LAN read-only UI)
After=network-online.target syzygy-guardian.service
Wants=network-online.target syzygy-guardian.service

[Service]
Type=simple
User=$account
WorkingDirectory=$root
Environment=MC_HOST=0.0.0.0
Environment=MC_PORT=9070
Environment=MC_STATE_DIR=$root/state
Environment=MC_UI_DIR=$root/ui
Environment=MC_HARD_STALE_S=120
ExecStart=$root/scripts/run-mission-control.sh
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
fi
sudo systemctl daemon-reload
sudo systemctl enable --now syzygy-agent.service
sudo systemctl restart syzygy-agent.service
if [[ "$role" == pi ]]; then
  sudo systemctl enable --now syzygy-guardian.service
  sudo systemctl restart syzygy-guardian.service
  if [[ -x "$root/scripts/run-mission-control.sh" ]]; then
    sudo systemctl enable --now syzygy-mission-control.service
    sudo systemctl restart syzygy-mission-control.service
  fi
fi
