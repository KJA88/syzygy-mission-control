#!/usr/bin/env bash
# Fitbit feeder. Writes only through the loopback workbook service.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -z "${HEALTH_WORKBOOK_MAINTAIN_TOKEN:-}" ]]; then
  echo "HEALTH_WORKBOOK_MAINTAIN_TOKEN is required" >&2
  exit 1
fi

export HEALTH_WORKBOOK_URL="${HEALTH_WORKBOOK_URL:-http://127.0.0.1:5052}"
export FITBIT_MCP_URL="${FITBIT_MCP_URL:-http://127.0.0.1:8010/mcp}"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="${PYTHON:-/usr/bin/python3}"
fi

exec "$PYTHON" -m health_workbook.fitbit_feeder
