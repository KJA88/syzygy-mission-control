"""Read the Macro App database and write current meals through the workbook service."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

from health_workbook.feeders import WorkbookClient, load_macro_entries, sync_dates, sync_macro

DEFAULT_DB = "/home/KA_PI/syzygy-macros/instance/macros.sqlite3"


def main() -> int:
    token = os.environ.get("HEALTH_WORKBOOK_MAINTAIN_TOKEN", "")
    if not token:
        print(json.dumps({"feeder": "macro", "errors": 1, "failed": "missing_token"}))
        return 1
    dates = sync_dates()
    recorded_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        entries = load_macro_entries(os.environ.get("MACRO_DB", DEFAULT_DB), dates)
    except sqlite3.Error:
        print(json.dumps({"feeder": "macro", "errors": 1, "failed": "macro_db"}))
        return 1
    workbook = WorkbookClient(os.environ.get("HEALTH_WORKBOOK_URL", "http://127.0.0.1:5052"), token)
    try:
        result = sync_macro(entries, workbook, dates, recorded_at)
    except Exception:
        print(json.dumps({"feeder": "macro", "errors": 1, "failed": "sync_failed"}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
