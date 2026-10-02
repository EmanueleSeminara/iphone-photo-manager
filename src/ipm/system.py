"""Thin wrappers around the few OS interactions the app needs."""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

__all__ = ["DiskSpace", "disk_space", "open_in_file_manager"]

logger = logging.getLogger(__name__)


KEEP_FREE = 2 * 1024**3
"""Headroom to leave on the volume, over and above what an import needs."""


@dataclass(frozen=True, slots=True)
class DiskSpace:
    """How much room the destination's volume has left."""

    free: int
    total: int

    @property
    def used(self) -> int:
        """Bytes in use on that volume."""
        return max(self.total - self.free, 0)

    def fits(self, needed: int, margin: int = KEEP_FREE) -> bool:
        """Whether *needed* bytes would fit and still leave the volume some room.

        The margin exists because filling a startup disk completely is its own
        kind of disaster, and an import is the one thing here that can do it.
        """
        return self.free - needed >= margin


def disk_space(path: Path) -> DiskSpace | None:
    """Return the free and total bytes of the volume holding *path*.

    Walks up to the nearest existing parent, so a destination that has not been
    created yet still reports the volume it would land on.

    :return: ``None`` when nothing about the path can be stat-ed.
    """
    for candidate in [path, *path.parents]:
        try:
            usage = shutil.disk_usage(candidate)
        except OSError:
            continue
        return DiskSpace(free=usage.free, total=usage.total)
    return None


def open_in_file_manager(path: Path) -> bool:
    """Reveal *path* in the platform's file manager.

    macOS uses ``open``; Linux ``xdg-open`` is tried as a courtesy for future ports.
    Failure is never fatal -- the import already succeeded by the time this runs.

    :return: True when a file manager was launched.
    """
    if not path.exists():
        return False

    if sys.platform == "darwin":
        command = "open"
    elif sys.platform.startswith("linux"):
        command = "xdg-open"
    else:
        return False

    executable = shutil.which(command)
    if executable is None:
        logger.info("%s is not available; not opening %s", command, path)
        return False

    try:
        subprocess.Popen(  # noqa: S603 - fixed command, path passed as an argument
            [executable, str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.info("could not open %s: %s", path, exc)
        return False
    return True
