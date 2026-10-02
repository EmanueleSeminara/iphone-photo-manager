"""Grouping manifest rows into the *items* the Photos app shows.

AFC deals in files, the Photos app deals in items, and a Live Photo is one item made
of two files (``IMG_0001.HEIC`` plus ``IMG_0001.MOV``) often with a third, the
``IMG_0001.AAE`` sidecar holding the non-destructive edits. Every decision about
*what to remove* is therefore taken over items, never over loose files: deleting
"the 5 most recent files" could strand a Live Photo's video half on the phone or
orphan a sidecar, while deleting "the 5 most recent items" cannot.

This module holds that grouping on its own so both the deletion rules
(:mod:`ipm.core.deleter`) and the join with the phone's own library
(:mod:`ipm.core.assets`) can use it without depending on each other.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from ipm.core.library import item_key
from ipm.models import ImportRecord, RemoteFile

__all__ = [
    "ItemGroup",
    "capture_times",
    "group_by_item",
    "select_newest_items",
    "sort_newest_first",
]


def capture_times(device_files: Iterable[RemoteFile]) -> dict[str, float]:
    """Return, for each device path, the earliest timestamp the phone reports for it.

    A file has two: when it was created (``st_birthtime``) and when it was last
    written (``st_mtime``). For a photo straight from the camera they agree. The
    modification time alone is not to be trusted for "when was this taken": measured
    on an iPhone 13 on 2026-10-02, two PNGs saved in September (``IMG_7608.PNG``,
    ``IMG_7609.PNG``, created 2026-09) carried a modification time seven hours *in
    the future*, so "the most recent items" put them ahead of photos taken minutes
    earlier. The earlier of the two timestamps is the one closest to the moment the
    file came into being, and it cannot be pushed forward by an app touching the file
    later.

    :param device_files: A listing from the device.
    :return: Device path to POSIX timestamp.
    """
    times: dict[str, float] = {}
    for item in device_files:
        try:
            times[item.path] = min(item.created.timestamp(), item.modified.timestamp())
        except (OverflowError, OSError, ValueError):
            continue
    return times


@dataclass(frozen=True, slots=True)
class ItemGroup:
    """Every manifest row belonging to one Photos-app item.

    :ivar key: The ``(folder, base name)`` key from :func:`~ipm.core.library.item_key`.
    :ivar captured: When the item was taken -- the *oldest* device timestamp among its
        files. See :func:`group_by_item` for why the oldest and not the newest.
    :ivar records: The rows themselves, ordered by device path.
    """

    key: str
    captured: float
    records: tuple[ImportRecord, ...]

    @property
    def size(self) -> int:
        """Bytes the group occupies on the device."""
        return sum(record.size for record in self.records)

    @property
    def name(self) -> str:
        """Base name of the first file in the group, for messages."""
        return self.records[0].device_path.rsplit("/", 1)[-1] if self.records else self.key


def group_by_item(
    records: Iterable[ImportRecord], taken: Mapping[str, float] | None = None
) -> list[ItemGroup]:
    """Group manifest rows into Photos-app items, most recently taken first.

    Grouping is what makes a partial deletion safe to offer: deleting "the 5 most
    recent items" must never split a Live Photo, leaving its ``.MOV`` half orphaned
    on the phone.

    **An item is dated by its oldest file, not its newest**, and that is worth the
    paragraph. A file's timestamp is when iOS *finished writing* it, not when the
    shutter went. For a Live Photo the video half is written as it is captured, while
    the still goes through the imaging pipeline first and lands seconds later -- and
    the pipeline does not preserve the order. Measured on an iPhone 13 (iOS 26.6),
    three Live Photos taken four seconds apart::

        IMG_7128.MOV   23:22:25      IMG_7128.HEIC   23:22:49
        IMG_7129.MOV   23:22:29      IMG_7129.HEIC   23:22:48
        IMG_7130.MOV   23:22:33      IMG_7130.HEIC   23:22:47

    The video halves are in the order the photos were taken; the stills came out
    backwards. Dating each item by its newest file therefore made ``IMG_7128`` -- the
    *first* of the three -- look like the most recent item on the phone, and "delete
    the newest item" removed the oldest of the burst. The oldest file in a group is
    the one written closest to the moment of capture, so that is the one that dates
    the item.

    The same reasoning covers the ``.AAE`` sidecar: editing a photo from last year
    writes a sidecar today, and the item is still a photo from last year.

    **And a file is dated by the earlier of its two timestamps** when *taken* gives
    them (:func:`capture_times`): a modification time can be wrong, even in the
    future, while the creation time says when the file appeared. Without *taken* the
    manifest's modification time is all there is.

    :param records: Rows to group (typically the deletable candidates).
    :param taken: Device path to the time the file was taken, from
        :func:`capture_times`. Paths missing from it fall back to the manifest's
        modification time.
    :return: One :class:`ItemGroup` per item, most recently taken first; exact ties
        broken by key, highest first, because iOS numbers photos in the order they
        are taken within a folder.
    """
    buckets: dict[str, list[ImportRecord]] = {}
    for record in records:
        buckets.setdefault(item_key(record.device_path), []).append(record)

    return sort_newest_first(
        [
            ItemGroup(
                key=key,
                captured=min(_taken(record, taken) for record in rows),
                records=tuple(sorted(rows, key=lambda record: record.device_path)),
            )
            for key, rows in buckets.items()
        ]
    )


def sort_newest_first(groups: list[ItemGroup]) -> list[ItemGroup]:
    """Order items most recently taken first, in place, and return them.

    Two passes over a stable sort rather than one composite key: the tie-break has to
    run *descending* on the key, so that two photos stamped with the same second are
    ordered by the number iOS gave them and ``IMG_7130`` comes before ``IMG_7129``. A
    composite ``(-captured, key)`` would put them the other way round.
    """
    groups.sort(key=lambda group: group.key, reverse=True)
    groups.sort(key=lambda group: group.captured, reverse=True)
    return groups


def select_newest_items(
    records: Iterable[ImportRecord],
    limit: int | None = None,
    taken: Mapping[str, float] | None = None,
) -> list[ImportRecord]:
    """Return the rows making up the *limit* most recent items.

    :param records: Candidate rows (already verified as deletable).
    :param limit: Number of *items* to keep; ``None`` or a number at least as large
        as the item count means "everything". Zero or less selects nothing.
    :param taken: When each file was taken, from :func:`capture_times`; see
        :func:`group_by_item`.
    :return: The selected rows, ordered by device path.
    """
    groups = group_by_item(records, taken)
    if limit is not None:
        groups = groups[: max(limit, 0)]
    selected = [record for group in groups for record in group.records]
    selected.sort(key=lambda record: record.device_path)
    return selected


def taken_at(record: ImportRecord, taken: Mapping[str, float] | None) -> float:
    """When *record*'s file was taken: from *taken* when it knows, else its mtime."""
    return _taken(record, taken)


def _taken(record: ImportRecord, taken: Mapping[str, float] | None) -> float:
    if taken is not None:
        stamp = taken.get(record.device_path)
        if stamp is not None:
            return stamp
    return record.mtime
