"""A plain-text copy of what the activity panel showed, kept between runs.

The panel on screen is the only record of what happened, and it goes when the app
does. That is fine while the only user is the person who wrote it; it stops being
fine the moment someone else opens an issue, because the first thing to ask for is
what the app said, and they have nothing to give.

So every line written to the panel is also appended here, without the console
markup, at ``<destination>/.ipm/last-run.log``. Previous runs are kept beside it as
``last-run.log.1`` and so on -- a bug is usually noticed one run too late.

**What it contains, and does not.** The same sentences that were on screen: device
paths, file names, counts, error messages. It does not hold a manifest or anything
from the phone's own database, so it is safe to attach to a bug report -- which is
the whole point of writing it.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from types import TracebackType

__all__ = ["LOG_NAME", "RunLog"]

LOG_NAME = "last-run.log"
KEEP_RUNS = 5
"""How many previous runs to keep. Small on purpose: these are notes, not history."""

logger = logging.getLogger(__name__)


class RunLog:
    """Appends the activity panel's lines to a file, rotating on each run.

    Every failure is swallowed and logged once: a destination on a read-only volume,
    or a folder the user has no permission to write to, must not stop an import.
    Losing the log is a nuisance; refusing to copy photos over it would not be.
    """

    def __init__(self, path: Path, keep: int = KEEP_RUNS) -> None:
        """
        :param path: File to write, e.g. ``<destination>/.ipm/last-run.log``.
        :param keep: Number of files to keep, counting the current one.
        """
        self.path = path
        self.keep = max(1, keep)
        self._handle: object | None = None
        self._complained = False

    # -- lifecycle ---------------------------------------------------------

    def open(self, header: str = "") -> None:
        """Rotate the previous runs out of the way and start a new file.

        :param header: One line describing the run, written first.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate()
            handle = self.path.open("w", encoding="utf-8")
        except OSError as exc:
            self._complain(exc)
            return
        self._handle = handle
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.write(f"{stamp}  {header}" if header else stamp)

    def _rotate(self) -> None:
        """Shift ``last-run.log`` to ``.1``, ``.1`` to ``.2``, and drop the oldest."""
        if not self.path.exists():
            return
        stem = self.path.name
        for index in range(self.keep - 1, 0, -1):
            older = self.path.with_name(f"{stem}.{index}")
            if index == self.keep - 1 and older.exists():
                older.unlink()
                continue
            if older.exists():
                older.rename(self.path.with_name(f"{stem}.{index + 1}"))
        self.path.rename(self.path.with_name(f"{stem}.1"))

    def close(self) -> None:
        """Flush and close, if it was ever opened."""
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            handle.close()  # type: ignore[attr-defined]
        except OSError as exc:
            self._complain(exc)

    def __enter__(self) -> RunLog:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- writing -----------------------------------------------------------

    def write(self, line: str) -> None:
        """Append one line, flushing immediately.

        Flushed every time on purpose: the run this file is most likely to be read
        after is the one that ended badly, and a buffered tail would be the part
        that mattered.
        """
        handle = self._handle
        if handle is None:
            return
        try:
            handle.write(line.rstrip("\n") + "\n")  # type: ignore[attr-defined]
            handle.flush()  # type: ignore[attr-defined]
        except OSError as exc:
            self._complain(exc)
            self._handle = None

    def _complain(self, exc: OSError) -> None:
        """Log the first failure and stay quiet about the rest."""
        if not self._complained:
            self._complained = True
            logger.info("cannot write the run log at %s: %s", self.path, exc)
