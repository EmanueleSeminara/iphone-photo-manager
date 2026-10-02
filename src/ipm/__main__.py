"""Allow ``python -m ipm`` in addition to the installed ``ipm`` command."""

from __future__ import annotations

from ipm.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
