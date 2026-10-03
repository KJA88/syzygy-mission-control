"""Idempotent Measurements sheet migration for the staged workbook."""

from __future__ import annotations

import os
from pathlib import Path
import sys

from health_workbook.store import WriteStore


def main() -> None:
    raw = os.environ.get("HEALTH_WORKBOOK_PATH", "")
    path = Path(raw)
    if not raw or not path.is_file() or path.is_symlink():
        raise SystemExit("HEALTH_WORKBOOK_PATH must be a regular file")
    result = WriteStore(path).migrate_measurements()
    print(result["result"])
    if result.get("backup"):
        print(result["backup"])


if __name__ == "__main__":
    sys.exit(main())
