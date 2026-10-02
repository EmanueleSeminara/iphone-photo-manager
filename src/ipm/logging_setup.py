"""Optional file logging.

Logging is off by default: the TUI owns the terminal, so anything written to
stdout/stderr would corrupt the display. When ``--log-file`` is passed (or a log
file is stored in the config) everything goes to that file and nowhere else.
"""

from __future__ import annotations

import logging
from pathlib import Path

__all__ = ["configure_logging", "default_log_path"]

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def default_log_path() -> Path:
    """Return the conventional location for the debug log."""
    return Path.home() / ".local" / "state" / "iphone-photo-manager" / "ipm.log"


def configure_logging(log_file: Path | None, *, verbose: bool = False) -> None:
    """Send library and application logs to *log_file*, or silence them entirely.

    :param log_file: Destination file; parent directories are created. ``None``
        disables logging (only a null handler is installed).
    :param verbose: When True, log at ``DEBUG`` level instead of ``INFO``.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    if log_file is None:
        root.addHandler(logging.NullHandler())
        root.setLevel(logging.CRITICAL)
        return

    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    # pymobiledevice3 is chatty at DEBUG and would flood the file with protocol dumps.
    logging.getLogger("pymobiledevice3").setLevel(logging.WARNING)
