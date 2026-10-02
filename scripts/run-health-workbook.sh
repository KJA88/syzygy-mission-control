#!/usr/bin/env bash
# Phase 1 read-only health workbook service. Loopback only.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export HEALTH_WORKBOOK_HOST=127.0.0.1
export HEALTH_WORKBOOK_PORT="${HEALTH_WORKBOOK_PORT:-5052}"

if [[ -z "${HEALTH_WORKBOOK_READ_TOKEN:-}" ]]; then
  echo "HEALTH_WORKBOOK_READ_TOKEN is required" >&2
  exit 1
fi
if [[ -z "${HEALTH_WORKBOOK_PATH:-}" ]]; then
  echo "HEALTH_WORKBOOK_PATH is required" >&2
  exit 1
fi
if [[ -L "$HEALTH_WORKBOOK_PATH" ]]; then
  echo "Staged workbook must be a regular file, not a symlink" >&2
  exit 1
fi

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="${PYTHON:-/usr/bin/python3}"
fi

echo "Starting health workbook service on http://127.0.0.1:${HEALTH_WORKBOOK_PORT}/"
echo "  mode=read-only-staging"
echo "  workbook=$HEALTH_WORKBOOK_PATH"

exec "$PYTHON" -m health_workbook
