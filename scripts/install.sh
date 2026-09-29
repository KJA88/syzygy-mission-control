#!/usr/bin/env bash
set -euo pipefail
role="${1:?Usage: bash scripts/install.sh pi|jetson}"
case "$role" in pi|jetson) ;; *) echo 'Expected pi or jetson' >&2; exit 2;; esac
root="$(cd "$(dirname "$0")/.." && pwd)"
case "$root" in *[[:space:]]*) echo 'Install from a repository path without whitespace' >&2; exit 2;; esac
account="$(id -un)"
if [[ "$account" == root ]]; then echo 'Run as your normal login user; sudo is used for unit installation only' >&2; exit 2; fi
cd "$root"
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
EnvironmentFile=-/etc/syzygy/guardian-auth.env
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

# The checked-in unit is authoritative, including the Home Assistant EnvironmentFile.
sudo cp "$root/systemd/syzygy-mission-control.service" /etc/systemd/system/syzygy-mission-control.service
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
