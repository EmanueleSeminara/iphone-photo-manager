"""SQLite manifest tracking what has been imported and what has been deleted.

The database lives inside the destination folder (``<destination>/.ipm/manifest.db``)
so a folder is self-describing: move it, and its import history moves with it.

Rationale for SQLite over a JSON file: the manifest is written once per copied file
while an import is running. A JSON file has to be rewritten in full every time (or
buffered and lost on a crash), whereas SQLite gives per-row durability, indexed
lookups over tens of thousands of rows, and a transaction boundary that survives the
Mac going to sleep mid-import.

The class is synchronous and guarded by a lock; async callers should wrap calls in
:func:`asyncio.to_thread` (see :class:`ipm.core.importer.Importer`).
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

from ipm.core.library import split_kinds
from ipm.core.organizer import is_safe_local_path
from ipm.errors import IpmError
from ipm.models import ImportRecord, KnownDevice, LibraryStatus, RemoteFile

__all__ = ["MANIFEST_DIRNAME", "MANIFEST_FILENAME", "ManifestDatabase", "manifest_path_for"]

MANIFEST_DIRNAME = ".ipm"
MANIFEST_FILENAME = "manifest.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    device_udid            TEXT NOT NULL,
    device_path            TEXT NOT NULL,
    size                   INTEGER NOT NULL,
    mtime                  REAL NOT NULL,
    local_path             TEXT NOT NULL,
    imported_at            TEXT NOT NULL,
    deleted_from_device_at TEXT,
    sha256                 TEXT,
    PRIMARY KEY (device_udid, device_path)
);

CREATE TABLE IF NOT EXISTS devices (
    device_udid  TEXT PRIMARY KEY,
    name         TEXT,
    product_type TEXT,
    ios_version  TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_files_local_path ON files (local_path);
CREATE INDEX IF NOT EXISTS idx_files_deleted ON files (deleted_from_device_at);
"""

SCHEMA_VERSION = 2
"""1: initial. 2: ``files.sha256`` and the ``devices`` table."""

_MIGRATIONS: dict[int, tuple[str, ...]] = {
    # From 1 to 2. The new column is nullable on purpose: rows written by an older
    # version have no digest, and inventing one from the local file would prove
    # nothing about what the device sent.
    1: (
        "ALTER TABLE files ADD COLUMN sha256 TEXT",
        """
        CREATE TABLE IF NOT EXISTS devices (
            device_udid  TEXT PRIMARY KEY,
            name         TEXT,
            product_type TEXT,
            ios_version  TEXT,
            first_seen   TEXT NOT NULL,
            last_seen    TEXT NOT NULL
        )
        """,
    ),
}


def manifest_path_for(destination: Path) -> Path:
    """Return the manifest path used for a given destination root."""
    return destination / MANIFEST_DIRNAME / MANIFEST_FILENAME


def _now() -> datetime:
    """Current UTC time; isolated so tests can reason about stored timestamps."""
    return datetime.now(UTC)


def _parse_dt(value: str | None) -> datetime | None:
    """Parse an ISO timestamp stored in the manifest, tolerating legacy values."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class ManifestDatabase:
    """Import manifest backed by SQLite.

    Usable as a context manager::

        with ManifestDatabase(manifest_path_for(dest)) as db:
            db.record_import(...)
    """

    def __init__(self, path: Path) -> None:
        """
        :param path: File to open or create; parent directories are created.
        """
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._initialise()

    def _initialise(self) -> None:
        """Create or migrate the schema, and enable the durability settings we rely on."""
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute("SELECT version FROM schema_info").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO schema_info (version) VALUES (?)", (SCHEMA_VERSION,))
                self._conn.commit()
                return
            self._migrate(int(row["version"]))

    def _migrate(self, version: int) -> None:
        """Bring an older manifest up to :data:`SCHEMA_VERSION`, in place.

        The manifest lives in the user's photo folder and is the only record of what
        has been imported, so it is never recreated from scratch: each step is applied
        in order, inside one transaction, and the version is bumped with it.

        :raises IpmError: when the file was written by a *newer* version, which this
            code cannot understand and must not guess at.
        """
        if version > SCHEMA_VERSION:
            raise IpmError(
                "This folder's import history was written by a newer version of "
                "iphone-photo-manager. Update the app, or pick another folder."
            )
        while version < SCHEMA_VERSION:
            for statement in _MIGRATIONS[version]:
                self._conn.execute(statement)
            version += 1
            self._conn.execute("UPDATE schema_info SET version = ?", (version,))
        self._conn.commit()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close the underlying connection."""
        with self._lock:
            self._conn.close()

    def __enter__(self) -> ManifestDatabase:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- writes ------------------------------------------------------------

    def record_import(
        self,
        *,
        device_udid: str,
        device_path: str,
        size: int,
        mtime: float,
        local_path: str,
        imported_at: datetime | None = None,
        sha256: str | None = None,
    ) -> ImportRecord:
        """Insert (or replace) the row describing a successfully copied file.

        Replacing matters for re-imports: if the local copy was deleted and the file
        is copied again, the new row must clear any previous ``deleted_from_device_at``.

        :param local_path: Path of the local copy *relative* to the destination root.
        :return: The record as stored.
        :raises ValueError: if *local_path* would escape the destination folder.
        """
        if not is_safe_local_path(local_path):
            raise ValueError(f"refusing to record a local path outside the folder: {local_path!r}")
        stamp = imported_at or _now()
        record = ImportRecord(
            device_udid=device_udid,
            device_path=device_path,
            size=size,
            mtime=mtime,
            local_path=local_path,
            imported_at=stamp,
            sha256=sha256,
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO files
                    (device_udid, device_path, size, mtime, local_path, imported_at,
                     deleted_from_device_at, sha256)
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT (device_udid, device_path) DO UPDATE SET
                    size = excluded.size,
                    mtime = excluded.mtime,
                    local_path = excluded.local_path,
                    imported_at = excluded.imported_at,
                    deleted_from_device_at = NULL,
                    sha256 = excluded.sha256
                """,
                (device_udid, device_path, size, mtime, local_path, stamp.isoformat(), sha256),
            )
            self._conn.commit()
        return record

    def record_digest(self, device_udid: str, device_path: str, sha256: str) -> None:
        """Store a digest computed after the fact, for a row that had none.

        Used when a deep verification hashes a file imported by an older version: the
        digest then describes the local copy as it is now, which is weaker evidence
        than one taken from the transfer, but makes every later check meaningful.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE files SET sha256 = ? WHERE device_udid = ? AND device_path = ? "
                "AND sha256 IS NULL",
                (sha256, device_udid, device_path),
            )
            self._conn.commit()

    def mark_deleted(self, device_udid: str, device_paths: Iterable[str]) -> int:
        """Flag files as no longer present on the device.

        :return: Number of rows updated.
        """
        stamp = _now().isoformat()
        paths = list(device_paths)
        if not paths:
            return 0
        with self._lock:
            cursor = self._conn.executemany(
                "UPDATE files SET deleted_from_device_at = ? "
                "WHERE device_udid = ? AND device_path = ?",
                [(stamp, device_udid, path) for path in paths],
            )
            self._conn.commit()
            return cursor.rowcount if cursor.rowcount != -1 else len(paths)

    def forget(self, device_udid: str, device_path: str) -> None:
        """Remove a row entirely (used when a local copy turns out to be gone)."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM files WHERE device_udid = ? AND device_path = ?",
                (device_udid, device_path),
            )
            self._conn.commit()

    # -- devices -----------------------------------------------------------

    def known_device(self, device_udid: str) -> KnownDevice | None:
        """Return what this folder remembers about a device, or ``None``.

        Used to tell "the phone you always import from" apart from one that has never
        been seen here -- the first hint that a folder and a device do not belong
        together, or that the phone has been through a restore.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM devices WHERE device_udid = ?", (device_udid,)
            ).fetchone()
        if row is None:
            return None
        return KnownDevice(
            device_udid=str(row["device_udid"]),
            name=row["name"],
            product_type=row["product_type"],
            ios_version=row["ios_version"],
            first_seen=_parse_dt(row["first_seen"]) or _now(),
            last_seen=_parse_dt(row["last_seen"]) or _now(),
        )

    def remember_device(
        self,
        device_udid: str,
        *,
        name: str | None = None,
        product_type: str | None = None,
        ios_version: str | None = None,
    ) -> None:
        """Record (or refresh) what is known about a device. ``first_seen`` is kept."""
        stamp = _now().isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO devices
                    (device_udid, name, product_type, ios_version, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (device_udid) DO UPDATE SET
                    name = excluded.name,
                    product_type = excluded.product_type,
                    ios_version = excluded.ios_version,
                    last_seen = excluded.last_seen
                """,
                (device_udid, name, product_type, ios_version, stamp, stamp),
            )
            self._conn.commit()

    # -- reads -------------------------------------------------------------

    def get(self, device_udid: str, device_path: str) -> ImportRecord | None:
        """Return the record for one device file, or ``None``."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM files WHERE device_udid = ? AND device_path = ?",
                (device_udid, device_path),
            ).fetchone()
        return _row_to_record(row) if row else None

    def records_for_device(self, device_udid: str) -> list[ImportRecord]:
        """Return every record belonging to one device, oldest import first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM files WHERE device_udid = ? ORDER BY imported_at, device_path",
                (device_udid,),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def live_records(self, device_udid: str) -> list[ImportRecord]:
        """Return records for files still believed to exist on the device."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM files "
                "WHERE device_udid = ? AND deleted_from_device_at IS NULL "
                "ORDER BY device_path",
                (device_udid,),
            ).fetchall()
        return [_row_to_record(row) for row in rows]

    def all_records(self) -> Iterator[ImportRecord]:
        """Iterate over every record in the manifest, regardless of device."""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM files ORDER BY imported_at").fetchall()
        yield from (_row_to_record(row) for row in rows)

    def known_local_paths(self) -> set[str]:
        """Return every local path already claimed by the manifest.

        Used by the collision-safe naming logic so two different device files never
        target the same local name, even before the first one has been written.
        """
        with self._lock:
            rows = self._conn.execute("SELECT local_path FROM files").fetchall()
        return {str(row["local_path"]) for row in rows}

    def status(
        self,
        destination: Path,
        device_udid: str | None = None,
        device_files: Iterable[RemoteFile] | None = None,
    ) -> LibraryStatus:
        """Summarise the manifest against the files actually on disk.

        A row counts as *verified* when the local copy exists with the recorded size,
        as *unverified* when the copy is missing or truncated (never deletable), and
        as *deleted* when the device copy was already removed by a previous run.

        The verified rows are also split into photos and videos, which is what the
        "On this Mac" panel shows: a Live Photo counts as one photo, because its
        ``.MOV`` half shares a base name with the still and no library item.

        When *device_files* is given, the same pass also answers the question the
        panel is really asked -- "how much of what is on the phone do I already
        have?" -- by counting the scanned files with no live row under their device
        path. A row marked deleted does not count as coverage: the file would be
        imported again if it came back, so calling it covered would be a lie.

        :param destination: Root the ``local_path`` values are relative to.
        :param device_udid: Restrict to one device; ``None`` counts everything.
        :param device_files: The latest device scan, when a phone is attached.
        """
        with self._lock:
            if device_udid is None:
                rows = self._conn.execute("SELECT * FROM files").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM files WHERE device_udid = ?", (device_udid,)
                ).fetchall()

        verified = verified_bytes = unverified = deleted = 0
        live_paths: set[str] = set()
        verified_local: list[str] = []
        for row in rows:
            record = _row_to_record(row)
            if record.deleted_from_device_at is not None:
                deleted += 1
                continue
            live_paths.add(record.device_path)
            if local_copy_is_intact(record, destination):
                verified += 1
                verified_bytes += record.size
                verified_local.append(record.local_path)
            else:
                unverified += 1

        # The split describes what is actually in the folder, so it is taken over
        # the verified rows only: a row whose local copy has gone is not a photo
        # the user has.
        photos, videos, other = split_kinds(verified_local)

        on_device_files = on_device_bytes = pending_files = pending_bytes = 0
        for entry in device_files or ():
            on_device_files += 1
            on_device_bytes += entry.size
            if entry.path not in live_paths:
                pending_files += 1
                pending_bytes += entry.size

        return LibraryStatus(
            verified=verified,
            verified_bytes=verified_bytes,
            unverified=unverified,
            deleted=deleted,
            on_device_files=on_device_files,
            on_device_bytes=on_device_bytes,
            pending_files=pending_files,
            pending_bytes=pending_bytes,
            photos=photos,
            videos=videos,
            other=other,
        )


def local_copy_is_intact(record: ImportRecord, destination: Path) -> bool:
    """Return True when the local copy exists and has the recorded size.

    This is the single gate that decides whether a device file may ever be deleted;
    it is re-evaluated at click time, never trusted from the import run. A row whose
    ``local_path`` points outside the destination folder is rejected outright rather
    than stat-ed (see :func:`~ipm.core.organizer.is_safe_local_path`).
    """
    if not is_safe_local_path(record.local_path):
        return False
    local = record.absolute_local_path(destination)
    try:
        stat = local.stat()
    except OSError:
        return False
    return stat.st_size == record.size


def _row_to_record(row: sqlite3.Row) -> ImportRecord:
    """Convert a database row into an :class:`ImportRecord`."""
    # Every connection runs the migration on open, so the column is always there.
    digest = row["sha256"]
    return ImportRecord(
        device_udid=str(row["device_udid"]),
        device_path=str(row["device_path"]),
        size=int(row["size"]),
        mtime=float(row["mtime"]),
        local_path=str(row["local_path"]),
        imported_at=_parse_dt(row["imported_at"]) or _now(),
        deleted_from_device_at=_parse_dt(row["deleted_from_device_at"]),
        sha256=str(digest) if digest else None,
    )
