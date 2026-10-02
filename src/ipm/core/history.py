"""Comparing what the manifest believes with what the phone actually holds.

The manifest is written over months; the phone changes underneath it. Most of that
is ordinary -- photos deleted in the Photos app, new ones taken. One case is not:
a device path that now holds a *different* file. iOS restarts its ``IMG_nnnn``
numbering after the phone is erased and restored, and the UDID that keys the
manifest does not change, so old rows can end up pointing at new photos.

Every safety rule in :mod:`ipm.core.deleter` already refuses to act on such a row.
This module exists so the user is *told*, instead of quietly seeing files marked
undeletable, and can let the app tidy the stale rows away.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from ipm.models import ImportRecord, RemoteFile

__all__ = ["LibraryChange", "detect_changes"]

_WIPE_THRESHOLD = 20
"""Below this many tracked files, "everything vanished" is not worth a dialog."""


@dataclass(slots=True)
class LibraryChange:
    """How the last scan compares with the rows the manifest still believes in."""

    identical: int = 0
    """Rows whose device file is still exactly the one that was imported."""
    changed: list[ImportRecord] = field(default_factory=list)
    """Rows whose device path now holds different bytes -- the dangerous case."""
    absent: list[ImportRecord] = field(default_factory=list)
    """Rows whose device file is simply gone (deleted in the Photos app, usually)."""

    @property
    def tracked(self) -> int:
        """Rows examined in total."""
        return self.identical + len(self.changed) + len(self.absent)

    @property
    def everything_vanished(self) -> bool:
        """True when the phone holds none of the files this folder imported.

        A restore, a wipe, or -- just as likely -- the wrong folder for this phone.
        """
        return self.tracked >= _WIPE_THRESHOLD and self.identical == 0 and not self.changed

    @property
    def needs_attention(self) -> bool:
        """True when the user should be told before doing anything else."""
        return bool(self.changed) or self.everything_vanished


def detect_changes(
    records: Iterable[ImportRecord], device_files: Iterable[RemoteFile]
) -> LibraryChange:
    """Compare live manifest rows against a device listing.

    :param records: Rows still believed to be on the device
        (:meth:`~ipm.core.database.ManifestDatabase.live_records`).
    :param device_files: The listing from the last scan.
    """
    on_device = {item.path: item for item in device_files}
    change = LibraryChange()
    for record in records:
        remote = on_device.get(record.device_path)
        if remote is None:
            change.absent.append(record)
        elif record.describes(remote):
            change.identical += 1
        else:
            change.changed.append(record)
    return change
