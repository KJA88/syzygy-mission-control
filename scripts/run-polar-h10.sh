#!/usr/bin/env bash
# Manual Polar H10 capture. Writes only through the loopback workbook service.
# Usage: run-polar-h10.sh discover | resting [seconds] | workout [seconds]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

COMMAND="${1:-discover}"
SECONDS_ARG="${2:-30}"

if [[ "$COMMAND" != "discover" && -z "${HEALTH_WORKBOOK_MAINTAIN_TOKEN:-}" && -f /home/KA_PI/syzygy-runtime/health-workbook.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source /home/KA_PI/syzygy-runtime/health-workbook.env
  set +a
fi

export HEALTH_WORKBOOK_URL="${HEALTH_WORKBOOK_URL:-http://127.0.0.1:5052}"

pick_python() {
  local candidate
  for candidate in "$ROOT/.venv/bin/python" /usr/bin/python3 python3; do
    if [[ -x "$candidate" || "$candidate" == "python3" ]]; then
      if "$candidate" -c "import bleak" >/dev/null 2>&1; then
        printf '%s\n' "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

PYTHON="$(pick_python)" || {
  echo '{"feeder":"polar-h10","error":"bleak_missing","errors":1}'
  exit 1
}

exec "$PYTHON" -m health_workbook.polar_h10 "$COMMAND" --seconds "$SECONDS_ARG"
