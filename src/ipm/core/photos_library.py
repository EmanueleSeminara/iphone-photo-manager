"""Reading the iOS Photos database to learn what AFC cannot see.

AFC exposes files; it cannot tell a live photo from one sitting in *Recently
Deleted*, because iOS only flags the deletion in its database and leaves the file
on disk for 30 days. The flag lives in ``ZASSET.ZTRASHEDSTATE``.

This module is pure SQL over a local copy of ``Photos.sqlite`` — the copying is the
device layer's job (:mod:`ipm.device.photos_db`) — so it is unit-tested against a
synthetic database.

Schema notes (iOS 17/18, verified on a real device):

* one row per library item in ``ZASSET``; ``ZDIRECTORY`` is jail-relative and holds
  no leading slash (``DCIM/100APPLE``), ``ZFILENAME`` the file name;
* ``ZTRASHEDSTATE`` 1 = in Recently Deleted, ``ZHIDDEN`` 1 = in the Hidden album,
  ``ZVISIBILITYSTATE`` non-zero = not shown in the main grid;
* the Photos app's "N Items" equals the rows where all three are 0.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from ipm.core.library import item_key
from ipm.errors import IpmError
from ipm.models import RemoteFile

__all__ = ["LibraryReconciliation", "LibrarySnapshot", "read_snapshot", "reconcile"]

_REQUIRED_COLUMNS = {"ZDIRECTORY", "ZFILENAME", "ZTRASHEDSTATE"}


@dataclass(frozen=True, slots=True)
class LibrarySnapshot:
    """What the device's own database says about the library."""

    total: int = 0
    visible: int = 0
    """Rows the Photos app counts in "N Items"."""
    trashed_keys: frozenset[str] = frozenset()
    hidden_keys: frozenset[str] = frozenset()
    visible_keys: frozenset[str] = frozenset()

    @property
    def trashed(self) -> int:
        """Items sitting in Recently Deleted (files still on disk)."""
        return len(self.trashed_keys)

    @property
    def hidden(self) -> int:
        """Items in the Hidden album."""
        return len(self.hidden_keys)


@dataclass(frozen=True, slots=True)
class LibraryReconciliation:
    """The difference between what is on disk and what the library holds."""

    snapshot: LibrarySnapshot
    trashed_on_disk: list[str] = field(default_factory=list)
    """Item keys present as files but already deleted in the Photos app."""
    missing_files: list[str] = field(default_factory=list)
    """Library items with no file on the device -- these would be lost."""
    untracked: list[str] = field(default_factory=list)
    """Files with no row in the database at all."""

    @property
    def is_complete(self) -> bool:
        """True when every library item has a file behind it."""
        return not self.missing_files


def read_snapshot(database: Path) -> LibrarySnapshot:
    """Read ``ZASSET`` from a local copy of ``Photos.sqlite``.

    Opened read-only *without* ``immutable``, so that a ``-wal`` file sitting next
    to it is replayed: with ``immutable=1`` SQLite skips WAL recovery and recent
    changes (a photo deleted a minute ago) are silently missing.

    :param database: Path to the copied database.
    :raises IpmError: if the file is not a readable Photos database.
    """
    if not database.exists():
        raise IpmError("The copy of the Photos database is missing.")
    try:
        # The path is percent-encoded: in a SQLite URI a bare "?" or "#" in the
        # file name would be read as the start of the query or fragment part, and
        # the database would be opened somewhere else entirely.
        connection = sqlite3.connect(f"file:{quote(str(database))}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise IpmError(f"Cannot open the Photos database: {exc}") from exc

    try:
        connection.row_factory = sqlite3.Row
        columns = {row[1] for row in connection.execute("PRAGMA table_info(ZASSET)")}
        if not columns:
            raise IpmError(
                "This iPhone's photo database has an unexpected layout (no ZASSET table); "
                "the library check is not available."
            )
        missing = _REQUIRED_COLUMNS - columns
        if missing:
            raise IpmError(
                "This iPhone's photo database has an unexpected layout "
                f"(missing {', '.join(sorted(missing))}); the library check is not available."
            )

        has_hidden = "ZHIDDEN" in columns
        has_visibility = "ZVISIBILITYSTATE" in columns
        # The interpolated parts are the two literals above, chosen by a boolean --
        # no value from the database or from the user reaches the SQL. Column names
        # cannot be bound as parameters, which is why this is built as a string.
        optional = (", ZHIDDEN" if has_hidden else "") + (
            ", ZVISIBILITYSTATE" if has_visibility else ""
        )
        query = f"SELECT ZDIRECTORY, ZFILENAME, ZTRASHEDSTATE{optional} FROM ZASSET"  # noqa: S608
        rows = connection.execute(query).fetchall()
    except sqlite3.Error as exc:
        raise IpmError(f"Cannot read the Photos database: {exc}") from exc
    finally:
        connection.close()

    trashed: set[str] = set()
    hidden: set[str] = set()
    visible: set[str] = set()
    total = 0
    for row in rows:
        directory = row["ZDIRECTORY"]
        filename = row["ZFILENAME"]
        if not directory or not filename:
            continue
        total += 1
        key = item_key(f"/{str(directory).strip('/')}/{filename}")
        if row["ZTRASHEDSTATE"]:
            trashed.add(key)
            continue
        if has_hidden and row["ZHIDDEN"]:
            hidden.add(key)
            continue
        if has_visibility and row["ZVISIBILITYSTATE"]:
            continue
        visible.add(key)

    return LibrarySnapshot(
        total=total,
        visible=len(visible),
        trashed_keys=frozenset(trashed),
        hidden_keys=frozenset(hidden),
        visible_keys=frozenset(visible),
    )


def reconcile(snapshot: LibrarySnapshot, files: Iterable[RemoteFile]) -> LibraryReconciliation:
    """Compare a device listing with the library snapshot.

    :param snapshot: Result of :func:`read_snapshot`.
    :param files: What :meth:`~ipm.device.base.DeviceBackend.list_media` returned.
    :return: Which items are trashed, which are missing, which files are unknown.
    """
    on_disk = {item_key(item.path) for item in files}
    known = snapshot.visible_keys | snapshot.trashed_keys | snapshot.hidden_keys
    return LibraryReconciliation(
        snapshot=snapshot,
        trashed_on_disk=sorted(on_disk & snapshot.trashed_keys),
        missing_files=sorted(snapshot.visible_keys - on_disk),
        untracked=sorted(on_disk - known),
    )
