"""Where each device file lands on disk: ``<destination>/YYYY/MM/<name>``.

Two rules matter and are unit-tested:

* the year/month folder comes from the creation date reported by the device
  (``st_birthtime``), never from the file name or extension;
* a name is never overwritten. A target that already exists is *adopted* (no bytes
  transferred, a manifest row written for it) only when it matches the device file
  on both size and modification time; anything else gets an incremental suffix and
  both files are kept.

The mtime half of that rule matters more than it looks. Every file this application
writes gets the device's mtime restored (:meth:`~ipm.core.importer.Importer._restore_mtime`),
so a file *it* wrote is recognisable. A file that merely happens to share a name and a
byte count -- likely once the destination is a personal archive that already holds
``IMG_0001.HEIC`` from some other camera -- is not adopted, because adopting it would
record it as the proof that the phone's photo is safely copied, and the delete button
believes that proof.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path, PurePosixPath

from ipm.models import MTIME_TOLERANCE, PlannedFile, RemoteFile

__all__ = [
    "choose_destination",
    "is_safe_local_path",
    "month_directory",
    "relative_local_path",
    "sanitise_name",
]

_ILLEGAL = {"/", "\\", "\0", ":"}
_MAX_SUFFIX_ATTEMPTS = 10_000


def is_safe_local_path(local_path: str) -> bool:
    """Return True when *local_path* stays inside the destination folder.

    Every value this application writes to the manifest comes from
    :func:`relative_local_path` and is safe by construction. The check exists for
    the values it *reads back*: the manifest is a plain SQLite file inside a folder
    the user owns, so a hand-edited or corrupted row could carry ``/etc/passwd`` or
    ``../../elsewhere``. Such a row would make ``root / local_path`` point outside
    the destination and let an unrelated file vouch for a photo that was never
    imported -- which is exactly the evidence the delete gate relies on.

    :param local_path: The manifest's ``local_path`` column (POSIX, relative).
    """
    if not local_path or "\0" in local_path or "\\" in local_path:
        return False
    pure = PurePosixPath(local_path)
    if pure.is_absolute():
        return False
    return ".." not in pure.parts


def month_directory(root: Path, created: datetime) -> Path:
    """Return ``root/YYYY/MM`` for a creation timestamp."""
    return root / f"{created.year:04d}" / f"{created.month:02d}"


def sanitise_name(name: str) -> str:
    """Make a device file name safe to use as a local file name.

    Device names (``IMG_0001.HEIC``) are already safe; this only guards against
    separators, control characters and the degenerate empty/dot cases.
    """
    cleaned = "".join("_" if char in _ILLEGAL or ord(char) < 32 else char for char in name).strip()
    if cleaned in ("", ".", ".."):
        return "unnamed"
    return cleaned


def relative_local_path(root: Path, destination: Path) -> str:
    """Return *destination* relative to *root*, as a POSIX string for the manifest."""
    return destination.relative_to(root).as_posix()


def _with_suffix_index(path: Path, index: int) -> Path:
    """Return ``name_1.ext`` style variants of *path*."""
    stem = path.stem
    # ``Path.suffix`` keeps only the last extension, which is what we want for
    # IMG_0001.HEIC; multi-dot names like IMG_0001.HEIC.AAE keep their first part
    # in ``stem``, so the result stays recognisable.
    return path.with_name(f"{stem}_{index}{path.suffix}")


def choose_destination(
    root: Path,
    remote: RemoteFile,
    reserved: set[Path] | None = None,
) -> PlannedFile:
    """Pick the local path for *remote*, avoiding collisions.

    :param root: Destination root chosen by the user.
    :param remote: File as reported by the device.
    :param reserved: Paths already claimed (by the manifest, or by earlier files in
        this same planning pass). Mutated in place: the chosen path is added to it.
    :return: A :class:`~ipm.models.PlannedFile` whose ``already_on_disk`` flag tells
        the importer whether any bytes still need to be transferred.
    """
    claimed = reserved if reserved is not None else set()
    directory = month_directory(root, remote.created)
    candidate = directory / sanitise_name(remote.name)

    for index in range(0, _MAX_SUFFIX_ATTEMPTS):
        target = candidate if index == 0 else _with_suffix_index(candidate, index)
        if target in claimed:
            continue
        existing = _stat_or_none(target)
        if existing is None:
            claimed.add(target)
            return PlannedFile(remote=remote, destination=target, already_on_disk=False)
        if _looks_like_the_same_file(existing, remote):
            # Same name, same size, same timestamp: an untracked copy of this very
            # file (typically a manifest that was deleted). Adopt it instead of
            # transferring the bytes again.
            claimed.add(target)
            return PlannedFile(remote=remote, destination=target, already_on_disk=True)
        # A different file that happens to share the name: keep both.

    raise RuntimeError(f"Could not find a free name for {remote.path} under {directory}")


def _looks_like_the_same_file(existing: os.stat_result, remote: RemoteFile) -> bool:
    """Return True when the local file can stand in for *remote* without copying.

    Size *and* modification time must agree. Size alone is not enough: it is the
    evidence the delete gate later relies on, and an unrelated photo of the same
    length would make the device's original look safely backed up.
    """
    if existing.st_size != remote.size:
        return False
    return abs(existing.st_mtime - remote.modified.timestamp()) <= MTIME_TOLERANCE


def _stat_or_none(path: Path) -> os.stat_result | None:
    """Return ``stat()`` for *path*, or ``None`` when it does not exist."""
    try:
        return path.stat()
    except OSError:
        return None
