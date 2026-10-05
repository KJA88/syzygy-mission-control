"""Idempotent workbook migrations for the staged workbook.

  python -m health_workbook.migrate              # add the Measurements sheet (original behavior)
  python -m health_workbook.migrate schema-v2    # Daily.day_status + formula-driven deficit logic
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

from health_workbook.store import WriteStore


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    raw = os.environ.get("HEALTH_WORKBOOK_PATH", "")
    path = Path(raw)
    if not raw or not path.is_file() or path.is_symlink():
        raise SystemExit("HEALTH_WORKBOOK_PATH must be a regular file")
    store = WriteStore(path)
    if args and args[0] == "schema-v2":
        result = store.migrate_schema_v2()
    elif not args:
        result = store.migrate_measurements()
    else:
        raise SystemExit("usage: python -m health_workbook.migrate [schema-v2]")
    print(result["result"])
    if result.get("backup"):
        print(result["backup"])


if __name__ == "__main__":
    sys.exit(main())
