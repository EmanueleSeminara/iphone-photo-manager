"""Plain data structures shared by every layer.

These types are deliberately free of any ``pymobiledevice3`` import so that the
business logic and its tests never need a device (or even the library) present.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

__all__ = [
    "MTIME_TOLERANCE",
    "DeepVerification",
    "DeleteCandidates",
    "DeleteRequest",
    "DeleteStats",
    "DeviceAsset",
    "DeviceInfo",
    "ImportPlan",
    "ImportRecord",
    "ImportStats",
    "KnownDevice",
    "LibraryStatus",
    "PlannedFile",
    "RemoteFile",
]

MTIME_TOLERANCE = 2.0
"""Seconds of slack when comparing timestamps.

Two things force a tolerance rather than an exact comparison: filesystems that store
mtimes at one- or two-second resolution, and the round trip through
:func:`os.utime`. Two seconds is far below the granularity that matters here -- a
photo's timestamp is its capture time, and no two captures on the same path are two
seconds apart.
"""


@dataclass(frozen=True, slots=True)
class RemoteFile:
    """A single file found on the device, as reported by AFC.

    :ivar path: Absolute path inside the AFC jail, e.g. ``/DCIM/100APPLE/IMG_0001.HEIC``.
    :ivar size: Size in bytes.
    :ivar created: Creation timestamp reported by the device (``st_birthtime``).
    :ivar modified: Modification timestamp reported by the device (``st_mtime``).
    """

    path: str
    size: int
    created: datetime
    modified: datetime

    @property
    def name(self) -> str:
        """Base name of the file, without any directory component."""
        return self.path.rsplit("/", 1)[-1]


@dataclass(frozen=True, slots=True)
class DeviceAsset:
    """One *library item* as the photo service on the phone sees it.

    This is the other half of the picture from :class:`RemoteFile`. AFC shows the
    file system -- paths, bytes, timestamps -- and can remove a file, but it cannot
    touch the library database, so a file deleted that way leaves an unusable row
    behind in the Photos app. The image-capture (PTP) side shows *items* instead, and
    asking it to delete one makes iOS remove the asset properly, exactly as Image
    Capture does.

    The two views name things differently: PTP hides the ``DCIM`` layout behind
    session-local names like ``202608_a/QSCU9090.JPG``, so :attr:`identifier` is
    meaningless outside the session that produced it and must never be stored. What
    *is* stable is :attr:`original_filename` -- the real ``IMG_6480.HEIC`` -- and it
    is what joins an asset back to a manifest row.

    :ivar identifier: Opaque handle for the item, valid only within one session.
    :ivar original_filename: The name the file has on the device file system.
    :ivar size: Size in bytes, as reported by the image-capture side.
    :ivar created: Capture timestamp, when the device reports one.
    """

    identifier: str
    original_filename: str
    size: int
    created: datetime | None = None

    @property
    def key(self) -> tuple[str, int]:
        """The ``(name, size)`` pair used to join an asset to a manifest row.

        Neither half is unique on its own -- names repeat once the ``IMG_nnnn``
        counter wraps or the phone is restored, and sizes repeat constantly -- but
        the pair was measured to be unique in both directions across a 16 000-item
        library, with no collisions on either side.
        """
        return (self.original_filename, self.size)


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """Human-readable summary of the connected device.

    Every field except :attr:`serial` may be ``None``: recent iOS releases restrict
    some lockdown values, and the UI simply hides whatever is missing.
    """

    serial: str
    name: str | None = None
    product_type: str | None = None
    model_name: str | None = None
    ios_version: str | None = None
    battery_percent: int | None = None
    storage_total: int | None = None
    storage_free: int | None = None
    udid: str | None = None

    @property
    def storage_used(self) -> int | None:
        """Bytes used on the device, or ``None`` when the totals are unavailable."""
        if self.storage_total is None or self.storage_free is None:
            return None
        return max(self.storage_total - self.storage_free, 0)


@dataclass(frozen=True, slots=True)
class KnownDevice:
    """A device this folder has seen before, as remembered in the manifest."""

    device_udid: str
    name: str | None
    product_type: str | None
    ios_version: str | None
    first_seen: datetime
    last_seen: datetime


@dataclass(frozen=True, slots=True)
class ImportRecord:
    """One row of the import manifest (the SQLite ``files`` table)."""

    device_udid: str
    device_path: str
    size: int
    mtime: float
    local_path: str
    """Path of the local copy, relative to the destination root."""
    imported_at: datetime
    deleted_from_device_at: datetime | None = None
    sha256: str | None = None
    """Digest of the bytes as they were copied; ``None`` for rows written before
    digests were recorded at all."""

    def absolute_local_path(self, root: Path) -> Path:
        """Resolve :attr:`local_path` against the destination *root*."""
        return root / self.local_path

    def describes(self, remote: RemoteFile) -> bool:
        """Return True when *remote* is still the file this row was written for.

        Size **and** modification time must agree. Size alone is not enough: iOS
        restarts its ``IMG_nnnn`` numbering from scratch after the phone is erased
        and restored, and the UDID does not change, so an old row can find a brand
        new photo sitting at its device path. Treating that as "already imported"
        would skip the new photo *and* mark it deletable on the strength of a copy
        of something else -- the one way this application could lose a picture.
        """
        if self.size != remote.size:
            return False
        return abs(self.mtime - remote.modified.timestamp()) <= MTIME_TOLERANCE


@dataclass(frozen=True, slots=True)
class PlannedFile:
    """A remote file paired with the local destination chosen for it."""

    remote: RemoteFile
    destination: Path
    already_on_disk: bool = False
    """True when the local file is already this exact file (same size, same mtime).

    It is *adopted*: no bytes are transferred, the copy is hashed from disk and a
    manifest row is written for it. This is what makes a folder whose manifest was
    lost or deleted recover in minutes instead of re-downloading everything.
    """


@dataclass(slots=True)
class ImportPlan:
    """Result of comparing the device content with the local manifest."""

    to_copy: list[PlannedFile] = field(default_factory=list)
    already_imported: int = 0
    total_bytes: int = 0
    """Bytes that still have to be transferred (excludes adopted and skipped files)."""

    @property
    def file_count(self) -> int:
        """Number of files the run will deal with, transferred or adopted."""
        return len(self.to_copy)

    @property
    def adopted(self) -> int:
        """Files already on disk: checked and recorded, never downloaded again."""
        return sum(1 for planned in self.to_copy if planned.already_on_disk)

    @property
    def to_transfer(self) -> int:
        """Files whose bytes really have to cross the cable."""
        return self.file_count - self.adopted


@dataclass(slots=True)
class ImportStats:
    """Outcome of an import run."""

    copied: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_copied: int = 0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False


@dataclass(slots=True)
class DeleteStats:
    """Outcome of a delete run.

    Everything here is measured *after* the fact, by re-scanning the device: the
    phone acknowledges a delete request before its library has finished acting on it,
    so the only honest source for "what is gone" is the file system afterwards.
    """

    deleted: int = 0
    """Files that really disappeared from the device."""
    items_deleted: int = 0
    """Library items every file of which disappeared."""
    failed: int = 0
    """Rows and items deliberately kept, each with a sentence in :attr:`errors`."""
    items_refused: int = 0
    """Items the library could not be asked about: no match, or an ambiguous one."""
    bytes_freed: int = 0
    errors: list[str] = field(default_factory=list)
    leftover: list[str] = field(default_factory=list)
    """Device paths still present after the deletion.

    Expected to be empty. A Live Photo's ``.MOV`` half and its ``.AAE`` sidecar have
    no library item of their own and should go when their still does; if iOS ever
    leaves them behind, this is where it shows up instead of being silently assumed.
    """
    untouched: list[str] = field(default_factory=list)
    """The part of :attr:`leftover` that belongs to items the phone left whole.

    A requested item none of whose files disappeared was ignored, not half-deleted:
    the photo is still on the phone, intact, and asking again is the remedy. Kept
    apart from the rest of :attr:`leftover` -- files left behind by an item that
    *did* go -- because the two call for different sentences. Measured on an iPhone
    13 on 2026-10-02: in one run five single photos went and five Live Photos were
    left whole, and the report called them halves of deleted photos.
    """
    items_untouched: int = 0
    """How many requested items the phone left whole (see :attr:`untouched`)."""
    cancelled: bool = False
    retried: bool = False
    """Whether the request had to be sent a second time.

    Measured on an iPhone 13: a delete request is sometimes accepted, acknowledged
    without an error, and then not acted on. Sending the identical request again
    works. Recorded so the log says it happened rather than hiding it.
    """


@dataclass(slots=True)
class DeleteCandidates:
    """Files eligible for deletion, and the ones deliberately held back.

    Every list except :attr:`deletable` is a reason to leave a file on the phone.
    They are kept apart because they mean different things to the user: a missing
    local copy is something to fix, a changed device file is something to worry
    about.
    """

    deletable: list[ImportRecord] = field(default_factory=list)
    unverified: list[ImportRecord] = field(default_factory=list)
    """The local copy is missing or the wrong size."""
    changed_on_device: list[ImportRecord] = field(default_factory=list)
    """The device path now holds a *different* file than the one imported."""
    absent_from_device: list[ImportRecord] = field(default_factory=list)
    """The manifest still lists them, but the last scan did not find them."""
    taken: dict[str, float] = field(default_factory=dict)
    """When each considered file was taken, by device path (see
    :func:`ipm.core.items.capture_times`). Orders "the most recent items"; empty
    when no listing was given, and then the manifest's modification time is used."""
    edited_on_device: list[ImportRecord] = field(default_factory=list)
    """Every file of an item that has an edit on the phone.

    The original was imported, the edit was not, so deleting the item would lose the
    edit. Kept until edits are imported too.
    """

    @property
    def total_bytes(self) -> int:
        """Bytes that would be freed on the device."""
        return sum(record.size for record in self.deletable)

    @property
    def held_back(self) -> int:
        """How many rows are being kept on the phone for any reason."""
        return (
            len(self.unverified)
            + len(self.changed_on_device)
            + len(self.absent_from_device)
            + len(self.edited_on_device)
        )


@dataclass(frozen=True, slots=True)
class DeleteRequest:
    """What the confirmation dialog decided: which rows, and how hard to check them."""

    records: list[ImportRecord]
    deep: bool = True
    """Compare the digest of every local copy before deleting anything."""


@dataclass(slots=True)
class DeepVerification:
    """Outcome of re-reading the local copies and comparing their digests."""

    verified: list[ImportRecord] = field(default_factory=list)
    """The local copy hashes exactly as it did when it was copied."""
    mismatched: list[ImportRecord] = field(default_factory=list)
    """The local copy is the right size but no longer the right bytes."""
    unhashed: list[ImportRecord] = field(default_factory=list)
    """Imported before digests existed: checked by size only, and hashed now."""
    unreadable: list[ImportRecord] = field(default_factory=list)
    """The local copy could not be read at all."""
    cancelled: bool = False

    @property
    def safe(self) -> list[ImportRecord]:
        """Rows that may be deleted from the phone, ordered by device path."""
        return sorted(self.verified + self.unhashed, key=lambda record: record.device_path)

    @property
    def rejected(self) -> int:
        """How many rows the deep check refused."""
        return len(self.mismatched) + len(self.unreadable)


@dataclass(frozen=True, slots=True)
class LibraryStatus:
    """Counters shown in the "local library" panel, derived from the manifest.

    The last two pairs are filled only when a phone is connected, because they
    compare the manifest with what the device is holding right now.

    :ivar verified: Rows whose local copy exists at the recorded size.
    :ivar unverified: Rows whose local copy is missing or the wrong size.
    :ivar deleted: Rows already removed from the device by a previous run.
    :ivar on_device_files: Files the last scan found on the phone.
    :ivar pending_files: Of those, the ones with no live row here yet.
    :ivar photos: Verified items that are photos, Live Photos included.
    :ivar videos: Verified items that are videos.
    :ivar other: Verified items of neither kind.
    """

    verified: int = 0
    verified_bytes: int = 0
    unverified: int = 0
    deleted: int = 0
    on_device_files: int = 0
    on_device_bytes: int = 0
    pending_files: int = 0
    pending_bytes: int = 0
    photos: int = 0
    videos: int = 0
    other: int = 0

    @property
    def verified_items(self) -> int:
        """Verified files grouped the way the Photos app counts them."""
        return self.photos + self.videos + self.other

    @property
    def total(self) -> int:
        """Total number of rows tracked in the manifest."""
        return self.verified + self.unverified + self.deleted

    @property
    def covered_files(self) -> int:
        """Files on the phone that this folder already holds a copy of."""
        return max(self.on_device_files - self.pending_files, 0)

    @property
    def is_complete(self) -> bool:
        """True when a phone is attached and nothing on it is still to import."""
        return self.on_device_files > 0 and self.pending_files == 0
