"""Persist mission runs beside the other Mission Control state files."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ACTIVE = frozenset({"TRIGGERED", "CHECKING", "RUNNING"})
HISTORY_LIMIT = 40


def _stamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class RunStore:
    def __init__(self, path):
        self.path = Path(path)
        self.runs = []
        self.load()

    def load(self):
        self.runs = []
        loaded = None
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                loaded = None
            rows = loaded.get("runs") if isinstance(loaded, dict) else None
            if isinstance(rows, list):
                self.runs = [row for row in rows if isinstance(row, dict)][:HISTORY_LIMIT]
        changed = False
        for run in self.runs:
            if run.get("state") in ACTIVE:
                now = _stamp()
                run["state"] = "FAULT"
                run["reason"] = "INTERRUPTED"
                run["updated_at"] = now
                run["completed_at"] = now
                changed = True
        if changed or (self.path.is_file() and not isinstance(loaded, dict)):
            self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"runs": self.runs[:HISTORY_LIMIT]}, indent=2) + "\n"
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(self.path)

    def add(self, run):
        self.runs.insert(0, run)
        del self.runs[HISTORY_LIMIT:]
        self.save()

    def touch(self):
        self.save()
