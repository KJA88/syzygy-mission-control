#!/usr/bin/env bash
# Withings feeder. Writes only through the loopback workbook service.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -z "${HEALTH_WORKBOOK_MAINTAIN_TOKEN:-}" ]]; then
  echo "HEALTH_WORKBOOK_MAINTAIN_TOKEN is required" >&2
  exit 1
fi
if [[ -z "${WITHINGS_CLIENT_ID:-}" || -z "${WITHINGS_CLIENT_SECRET:-}" || -z "${WITHINGS_REFRESH_TOKEN:-}" ]]; then
  echo "Withings OAuth credentials are required" >&2
  exit 1
fi

export HEALTH_WORKBOOK_URL="${HEALTH_WORKBOOK_URL:-http://127.0.0.1:5052}"
export WITHINGS_ENV_FILE="${WITHINGS_ENV_FILE:-/home/KA_PI/syzygy-runtime/withings.env}"

if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="${PYTHON:-/usr/bin/python3}"
fi

exec "$PYTHON" -m health_workbook.withings_feeder
