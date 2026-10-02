"""Import orchestration: plan, transfer in parallel, record, resume.

Guarantees this module is responsible for:

* **Nothing is ever overwritten or transcoded.** Bytes come from the backend and go
  to disk untouched; only the mtime is restored afterwards.
* **A partial file is never visible.** Every transfer writes to
  ``<name>.ipm-part`` and is promoted with :func:`os.replace`, which is atomic on
  the same filesystem. A crash, a sleep, or an unplugged cable leaves at most a
  ``.ipm-part`` leftover, which the next run removes.
* **Resume is free.** A file already recorded in the manifest *and* still intact on
  disk is skipped without touching the device.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from ipm.core.database import ManifestDatabase, local_copy_is_intact
from ipm.core.digest import digest_of
from ipm.core.organizer import choose_destination, relative_local_path
from ipm.device.base import DeviceBackend
from ipm.errors import friendly_message
from ipm.models import ImportPlan, ImportStats, PlannedFile, RemoteFile

__all__ = ["ImportProgress", "Importer", "PART_SUFFIX", "ProgressCallback"]

logger = logging.getLogger(__name__)

PART_SUFFIX = ".ipm-part"
_PROGRESS_INTERVAL = 0.1
"""Minimum seconds between two progress callbacks, to keep the UI cheap."""


@dataclass(frozen=True, slots=True)
class ImportProgress:
    """Snapshot handed to the UI while an import runs."""

    phase: str
    """``"scanning"``, ``"copying"`` or ``"done"``."""
    files_done: int = 0
    files_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    current: str = ""
    bytes_per_second: float = 0.0
    eta_seconds: float | None = None

    @property
    def fraction(self) -> float:
        """Completion ratio in ``0.0..1.0`` (byte-based, falling back to files)."""
        if self.bytes_total > 0:
            return min(1.0, self.bytes_done / self.bytes_total)
        if self.files_total > 0:
            return min(1.0, self.files_done / self.files_total)
        return 0.0


ProgressCallback = Callable[[ImportProgress], None]


class _Cancelled(Exception):
    """Internal signal raised from the chunk callback to abort a transfer."""


class Importer:
    """Copies everything the device holds that the destination does not yet have."""

    def __init__(
        self,
        backend: DeviceBackend,
        database: ManifestDatabase,
        destination: Path,
        *,
        concurrency: int = 3,
        on_progress: ProgressCallback | None = None,
        cancel: asyncio.Event | None = None,
    ) -> None:
        """
        :param backend: Device access layer (real AFC backend, or a fake in tests).
        :param database: Manifest for the destination folder.
        :param destination: Root folder; ``YYYY/MM`` subfolders are created under it.
        :param concurrency: Parallel transfers. 3 keeps the USB link busy without
            starving the device's AFC service.
        :param on_progress: Called (throttled) with an :class:`ImportProgress`.
        :param cancel: Set this event to stop the run at the next chunk boundary.
        """
        self.backend = backend
        self.database = database
        self.destination = destination
        self.concurrency = max(1, concurrency)
        self.on_progress = on_progress
        self.cancel = cancel or asyncio.Event()

        self._bytes_done = 0
        self._bytes_total = 0
        self._files_done = 0
        self._files_total = 0
        self._started = 0.0
        self._last_emit = 0.0
        self._current = ""
        self._lock = asyncio.Lock()

    # -- planning ----------------------------------------------------------

    async def build_plan(self, files: Iterable[RemoteFile]) -> ImportPlan:
        """Decide what still needs copying and where each file goes.

        Planning stats one local file per manifest row, so the whole pass runs in a
        worker thread: with a large library it would otherwise block the UI for
        seconds.

        :param files: Everything found on the device.
        :return: The plan; ``already_imported`` counts files skipped outright.
        """
        return await asyncio.to_thread(self._build_plan, list(files), self.backend.serial)

    def _build_plan(self, files: list[RemoteFile], udid: str) -> ImportPlan:
        """Synchronous body of :meth:`build_plan` (runs off the event loop)."""
        records = self.database.records_for_device(udid)
        by_path = {record.device_path: record for record in records}
        reserved = {self.destination / claimed for claimed in self.database.known_local_paths()}

        plan = ImportPlan()
        for remote in files:
            record = by_path.get(remote.path)
            if record is not None and record.describes(remote):
                if record.deleted_from_device_at is not None or local_copy_is_intact(
                    record, self.destination
                ):
                    # Already imported (and either still on disk, or already removed
                    # from the phone in a previous run): nothing to do.
                    plan.already_imported += 1
                    continue
                # The manifest knows the file but the local copy is gone or truncated:
                # re-import it, reusing the same local name.
                reserved.discard(record.absolute_local_path(self.destination))

            planned = choose_destination(self.destination, remote, reserved)
            plan.to_copy.append(planned)
            if not planned.already_on_disk:
                plan.total_bytes += remote.size
        return plan

    # -- execution ---------------------------------------------------------

    async def run(
        self,
        files: Iterable[RemoteFile] | None = None,
        plan: ImportPlan | None = None,
    ) -> ImportStats:
        """Scan (if needed), plan and execute the import.

        :param files: Pre-fetched device listing; omit to have the backend scan.
        :param plan: A plan already built by :meth:`build_plan` -- the UI builds one
            before asking the user to confirm, and passes it back here so the work is
            not repeated. Omit to plan from *files*.
        :return: Counters describing the run. Errors on individual files never abort
            the whole import; they are collected in :attr:`ImportStats.errors`.
        """
        self._started = time.monotonic()
        if plan is None:
            if files is None:
                self._emit(phase="scanning", force=True)
                files = await self.backend.list_media(on_progress=self._on_scan)
            plan = await self.build_plan(files)
        stats = ImportStats(skipped=plan.already_imported)

        self._files_total = plan.file_count
        self._bytes_total = plan.total_bytes
        self._files_done = 0
        self._bytes_done = 0
        self._emit(phase="copying", force=True)

        if not plan.to_copy:
            self._emit(phase="done", force=True)
            return stats

        queue: asyncio.Queue[PlannedFile] = asyncio.Queue()
        for planned in plan.to_copy:
            queue.put_nowait(planned)

        workers = [
            asyncio.create_task(self._worker(queue, stats), name=f"ipm-import-{index}")
            for index in range(min(self.concurrency, len(plan.to_copy)))
        ]
        try:
            await asyncio.gather(*workers)
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

        stats.cancelled = self.cancel.is_set()
        self._emit(phase="done", force=True)
        return stats

    async def _worker(self, queue: asyncio.Queue[PlannedFile], stats: ImportStats) -> None:
        """Consume planned files until the queue drains or the run is cancelled."""
        while not self.cancel.is_set():
            try:
                planned = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await self._transfer(planned, stats)
            finally:
                queue.task_done()

    async def _transfer(self, planned: PlannedFile, stats: ImportStats) -> None:
        """Copy one file, then record it. Errors are captured, not raised."""
        remote = planned.remote
        destination = planned.destination
        part = destination.with_name(destination.name + PART_SUFFIX)

        async with self._lock:
            self._current = remote.name
        try:
            await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)

            if planned.already_on_disk:
                # Bytes are already there (adopted copy); only the manifest is missing,
                # so the digest has to come from reading the file back.
                await asyncio.to_thread(self._restore_mtime, destination, remote)
                digest = await asyncio.to_thread(digest_of, destination)
            else:
                await asyncio.to_thread(_remove_quietly, part)
                # Hashing happens on the chunks as they arrive: the bytes are already
                # in memory, so a full verifiable digest costs nothing beyond the
                # hashing itself, and never a second pass over the disk.
                hasher = hashlib.sha256()

                def on_chunk(chunk: bytes) -> None:
                    hasher.update(chunk)
                    self._on_chunk(chunk)

                written = await self.backend.download_file(
                    remote.path, part, on_chunk=on_chunk
                )
                if written != remote.size:
                    raise OSError(
                        f"expected {remote.size} bytes from the device but received {written}"
                    )
                digest = hasher.hexdigest()
                await asyncio.to_thread(self._promote, part, destination, remote)

            await asyncio.to_thread(
                self.database.record_import,
                device_udid=self.backend.serial,
                device_path=remote.path,
                size=remote.size,
                mtime=remote.modified.timestamp(),
                local_path=relative_local_path(self.destination, destination),
                sha256=digest,
            )
        except (_Cancelled, asyncio.CancelledError):
            await asyncio.to_thread(_remove_quietly, part)
            self.cancel.set()
            return
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
            await asyncio.to_thread(_remove_quietly, part)
            logger.warning("import of %s failed", remote.path, exc_info=exc)
            async with self._lock:
                stats.failed += 1
                stats.errors.append(f"{remote.name}: {friendly_message(exc)}")
            return

        async with self._lock:
            self._files_done += 1
            if planned.already_on_disk:
                # Adopted copies transfer no bytes, and none were counted in the
                # plan total either, so the byte progress stays consistent.
                stats.skipped += 1
            else:
                stats.copied += 1
                stats.bytes_copied += remote.size
        self._emit(phase="copying")

    @staticmethod
    def _promote(part: Path, destination: Path, remote: RemoteFile) -> None:
        """Atomically move the completed temp file into place and restore its mtime."""
        os.replace(part, destination)
        Importer._restore_mtime(destination, remote)

    @staticmethod
    def _restore_mtime(destination: Path, remote: RemoteFile) -> None:
        """Give the local copy the modification time reported by the device."""
        with contextlib.suppress(OSError):
            timestamp = remote.modified.timestamp()
            os.utime(destination, (timestamp, timestamp))

    # -- progress ----------------------------------------------------------

    def _on_scan(self, files_seen: int, bytes_seen: int) -> None:
        """Backend scan callback."""
        self._files_total = files_seen
        self._bytes_total = bytes_seen
        self._emit(phase="scanning")

    def _on_chunk(self, chunk: bytes) -> None:
        """Backend transfer callback; raises :class:`_Cancelled` on request."""
        if self.cancel.is_set():
            raise _Cancelled
        self._bytes_done += len(chunk)
        self._emit(phase="copying")

    def _emit(self, *, phase: str, force: bool = False) -> None:
        """Send a throttled progress snapshot to the UI."""
        if self.on_progress is None:
            return
        now = time.monotonic()
        if not force and now - self._last_emit < _PROGRESS_INTERVAL:
            return
        self._last_emit = now
        elapsed = max(now - self._started, 1e-6)
        speed = self._bytes_done / elapsed
        remaining = max(self._bytes_total - self._bytes_done, 0)
        eta = remaining / speed if speed > 0 and remaining > 0 else None
        self.on_progress(
            ImportProgress(
                phase=phase,
                files_done=self._files_done,
                files_total=self._files_total,
                bytes_done=self._bytes_done,
                bytes_total=self._bytes_total,
                current=self._current,
                bytes_per_second=speed,
                eta_seconds=eta,
            )
        )


def _remove_quietly(path: Path) -> None:
    """Delete *path* if it exists, ignoring every filesystem error."""
    with contextlib.suppress(OSError):
        path.unlink()


def cleanup_partials(destination: Path) -> int:
    """Delete leftover ``*.ipm-part`` files under *destination*.

    Called before an import so an interrupted previous run leaves no debris.

    :return: Number of files removed.
    """
    removed = 0
    if not destination.exists():
        return 0
    for path in destination.rglob(f"*{PART_SUFFIX}"):
        if path.is_file():
            _remove_quietly(path)
            removed += 1
    return removed
