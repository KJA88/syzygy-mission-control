#!/usr/bin/env bash
# Localhost SYZYGY capability registry. Health tokens come from the existing env file.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="${PYTHON:-/usr/bin/python3}"
fi

export SYZYGY_REGISTRY_HOST="${SYZYGY_REGISTRY_HOST:-127.0.0.1}"
export SYZYGY_REGISTRY_PORT="${SYZYGY_REGISTRY_PORT:-8076}"
export HEALTH_WORKBOOK_URL="${HEALTH_WORKBOOK_URL:-http://127.0.0.1:5052}"

exec "$PYTHON" -m registry.mcp_server
