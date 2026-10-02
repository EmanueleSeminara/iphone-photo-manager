"""Translating a list of device files into the number the Photos app shows.

The Photos app counts *items*; AFC exposes *files*, and one item is often several
files. Verified against a real iPhone 13 (iOS 18) by reconciling the device listing
against ``PhotoData/Photos.sqlite``:

* 16174 rows in ``ZASSET`` (what "16.169 Items" in the Photos app counts);
* 232 files on disk with no matching row: 220 ``.MOV`` halves of Live Photos and
  12 ``.AAE`` edit sidecars;
* every remaining file mapped 1:1 to a row.

So an item is exactly one *(folder, base name)* group: the Live Photo's `.HEIC` and
`.MOV` share a base name and collapse into one item, an `.AAE` collapses onto the
photo it belongs to, while a real video (`IMG_1235.MOV`, no image beside it) has a
base name of its own and counts as one item.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ipm.models import RemoteFile

__all__ = ["LibraryCounts", "count_items", "item_key", "split_kinds"]

SIDECAR_SUFFIXES = frozenset({".AAE"})
"""Extensions that are never an item of their own."""

VIDEO_SUFFIXES = frozenset({".MOV", ".MP4", ".M4V", ".AVI", ".QT"})
"""Extensions that make an item a video, when nothing else in it is a photo."""

PHOTO_SUFFIXES = frozenset(
    {".HEIC", ".HEIF", ".JPG", ".JPEG", ".PNG", ".GIF", ".WEBP", ".AVIF", ".DNG", ".TIF", ".TIFF"}
)
"""Extensions that make an item a photo, whatever else it is made of."""


def _suffix(path: str) -> str:
    """Upper-case extension of *path*, or ``""``."""
    name = path.rpartition("/")[2]
    stem, dot, suffix = name.rpartition(".")
    return f".{suffix.upper()}" if dot and stem else ""


def item_key(path: str) -> str:
    """Return the *(folder, base name)* key identifying the item a file belongs to.

    :param path: Absolute device path, e.g. ``/DCIM/100APPLE/IMG_0001.MOV``.
    :return: The key, e.g. ``/DCIM/100APPLE/IMG_0001``.
    """
    folder, _, name = path.rpartition("/")
    stem, dot, _suffix = name.rpartition(".")
    return f"{folder}/{stem if dot else name}"


@dataclass(frozen=True, slots=True)
class LibraryCounts:
    """How many files, and how many Photos-app items, a listing represents.

    :ivar photos: Items whose content is a still, Live Photos included.
    :ivar videos: Items that are a video and nothing else.
    :ivar other: Items of neither kind -- an unknown extension, or a sidecar whose
        photo is no longer on the phone.
    """

    files: int = 0
    items: int = 0
    bytes_total: int = 0
    photos: int = 0
    videos: int = 0
    other: int = 0

    @property
    def extra_files(self) -> int:
        """Files that share an item with another file (Live Photo halves, sidecars)."""
        return max(self.files - self.items, 0)


def count_items(files: Iterable[RemoteFile]) -> LibraryCounts:
    """Count files, the items they add up to, and how those items split.

    **A Live Photo counts as a photo, not a video.** Its ``.MOV`` half has no
    library item of its own -- the Photos app shows one photo, and so does this.
    Counting the half as a video would contradict the item model the whole
    application is built on, and on a real iPhone 13 it would have moved 232 items
    into the wrong column.

    :param files: Everything found on the device.
    :return: File count, item count, total size, and the photo/video split.
    """
    paths = []
    count = 0
    size = 0
    for item in files:
        count += 1
        size += item.size
        paths.append(item.path)

    photos, videos, other = split_kinds(paths)
    return LibraryCounts(
        files=count,
        items=photos + videos + other,
        bytes_total=size,
        photos=photos,
        videos=videos,
        other=other,
    )


def split_kinds(paths: Iterable[str]) -> tuple[int, int, int]:
    """Group *paths* into items and say how many are photos, videos and neither.

    Works on device paths and on local ones alike: a Live Photo's two halves share
    a base name in the same folder either way, which is the only thing the grouping
    depends on.

    :param paths: File paths belonging to one library.
    :return: ``(photos, videos, other)``, which always sums to the item count.
    """
    groups: dict[str, set[str]] = {}
    for path in paths:
        groups.setdefault(item_key(path), set()).add(_suffix(path))

    photos = videos = other = 0
    for suffixes in groups.values():
        content = suffixes - SIDECAR_SUFFIXES
        if content & PHOTO_SUFFIXES:
            photos += 1
        elif content & VIDEO_SUFFIXES:
            videos += 1
        else:
            other += 1
    return photos, videos, other
