#!/usr/bin/env bash
# Localhost Home Assistant MCP. The token is loaded by systemd EnvironmentFile.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="${PYTHON:-/usr/bin/python3}"
fi

export HA_MCP_HOST="${HA_MCP_HOST:-127.0.0.1}"
export HA_MCP_PORT="${HA_MCP_PORT:-8095}"

exec "$PYTHON" -m home.mcp_server
