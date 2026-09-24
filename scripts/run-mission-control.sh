#!/usr/bin/env bash
# SYZYGY Mission Control V0.1 launcher (LAN-only, read-only).
# Run from the syzygy-mission-control repo root on Pi.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

HOST="${MC_HOST:-0.0.0.0}"
PORT="${MC_PORT:-9070}"
STATE_DIR="${MC_STATE_DIR:-$ROOT/state}"
UI_DIR="${MC_UI_DIR:-$ROOT/ui}"
HARD_STALE_S="${MC_HARD_STALE_S:-120}"

# Prefer repo venv if present; otherwise system python3.
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="${PYTHON:-/usr/bin/python3}"
fi

mkdir -p "$STATE_DIR"

echo "Starting Mission Control on http://${HOST}:${PORT}/"
echo "  state-dir=$STATE_DIR"
echo "  ui-dir=$UI_DIR"
echo "  hard-stale-s=$HARD_STALE_S"
echo "  LAN-only — do not publish via Cloudflare"
echo "  Open e.g. http://192.168.1.18:${PORT}/ from a LAN browser"

exec "$PYTHON" "$UI_DIR/server.py" \
  --host "$HOST" \
  --port "$PORT" \
  --state-dir "$STATE_DIR" \
  --ui-dir "$UI_DIR" \
  --hard-stale-s "$HARD_STALE_S"
