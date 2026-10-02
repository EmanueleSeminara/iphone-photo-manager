"""Fetching a copy of the device's Photos database.

Kept apart from :mod:`ipm.core.photos_library` (which only reads a local file) so the
reading logic stays testable without a phone.

The database is big -- 2.25 GB on a 16 000-item library, ~70 s over USB 2 -- so this
is only ever done on explicit request, never as part of a scan.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path

from ipm.device.base import DeviceBackend

__all__ = ["PHOTOS_DB", "PhotosDatabaseCopy", "database_size"]

logger = logging.getLogger(__name__)

PHOTOS_DB = "/PhotoData/Photos.sqlite"
_SIDECARS = ("-wal",)
"""Companions worth pulling. ``-shm`` is rebuilt by SQLite; ``-wal`` is not, and
without it a recent change (a photo deleted a minute ago) would be invisible."""


async def database_size(backend: DeviceBackend) -> int:
    """Return the size of the Photos database in bytes, or 0 when unavailable."""
    try:
        return await backend.file_size(PHOTOS_DB)
    except Exception as exc:  # noqa: BLE001 - purely informational
        logger.debug("cannot stat %s: %s", PHOTOS_DB, exc)
        return 0


class PhotosDatabaseCopy:
    """Async context manager yielding a local copy of ``Photos.sqlite``.

    The copy lands in a temporary directory that is removed on exit: it holds the
    metadata of every photo on the device and has no reason to outlive the check.
    :func:`tempfile.mkdtemp` creates it with ``0700`` permissions, so no other user
    on the Mac can read it while the check runs.

    ::

        async with PhotosDatabaseCopy(backend) as database:
            snapshot = read_snapshot(database)
    """

    def __init__(
        self,
        backend: DeviceBackend,
        on_chunk: Callable[[bytes], None] | None = None,
    ) -> None:
        """
        :param backend: Connected device.
        :param on_chunk: Forwarded to the download, for progress reporting.
        """
        self.backend = backend
        self.on_chunk = on_chunk
        self._directory: Path | None = None

    async def __aenter__(self) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="ipm-photosdb-"))
        self._directory = directory
        target = directory / "Photos.sqlite"
        await self.backend.download_file(PHOTOS_DB, target, on_chunk=self.on_chunk)
        for suffix in _SIDECARS:
            with contextlib.suppress(Exception):
                await self.backend.download_file(
                    PHOTOS_DB + suffix, directory / f"Photos.sqlite{suffix}"
                )
        return target

    async def __aexit__(self, *exc_info: object) -> None:
        if self._directory is not None:
            shutil.rmtree(self._directory, ignore_errors=True)
            self._directory = None
