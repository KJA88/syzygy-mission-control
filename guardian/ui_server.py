"""Optional module entry: python -m guardian.ui_server

Delegates to ui/server.py so the Mission Control server stays a single
stdlib implementation. Drop this file next to the existing guardian package.
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path


def main(argv=None):
    server = Path(__file__).resolve().parents[1] / "ui" / "server.py"
    if not server.is_file():
        sys.stderr.write("ui/server.py not found at %s\n" % server)
        return 2
    # Preserve argv for argparse inside ui/server.py
    if argv is not None:
        sys.argv = [str(server)] + list(argv)
    else:
        sys.argv = [str(server)] + sys.argv[1:]
    runpy.run_path(str(server), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
