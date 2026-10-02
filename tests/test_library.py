"""Files -> Photos-app items.

The expectations here come from a reconciliation against a real iPhone's
``PhotoData/Photos.sqlite``: 16174 assets, 16393 files under ``/DCIM``, and exactly
232 files with no asset row (220 Live Photo ``.MOV`` halves + 12 ``.AAE``).
"""

from __future__ import annotations

from datetime import datetime

from ipm.core.library import count_items, item_key
from ipm.models import RemoteFile

STAMP = datetime(2024, 3, 14, 9, 0, 0)


def remote(path: str, size: int = 10) -> RemoteFile:
    return RemoteFile(path=path, size=size, created=STAMP, modified=STAMP)


def test_item_key_strips_the_extension() -> None:
    assert item_key("/DCIM/100APPLE/IMG_0001.HEIC") == "/DCIM/100APPLE/IMG_0001"
    assert item_key("/DCIM/100APPLE/IMG_0001.MOV") == "/DCIM/100APPLE/IMG_0001"


def test_item_key_keeps_the_folder() -> None:
    assert item_key("/DCIM/100APPLE/IMG_1.JPG") != item_key("/DCIM/101APPLE/IMG_1.JPG")


def test_item_key_handles_a_name_without_extension() -> None:
    assert item_key("/DCIM/100APPLE/README") == "/DCIM/100APPLE/README"


def test_live_photo_pair_is_one_item() -> None:
    counts = count_items(
        [remote("/DCIM/100APPLE/IMG_0001.HEIC"), remote("/DCIM/100APPLE/IMG_0001.MOV")]
    )
    assert counts.files == 2
    assert counts.items == 1
    assert counts.extra_files == 1


def test_aae_sidecar_collapses_onto_its_photo() -> None:
    counts = count_items(
        [remote("/DCIM/100APPLE/IMG_0002.JPG"), remote("/DCIM/100APPLE/IMG_0002.AAE")]
    )
    assert counts.items == 1


def test_standalone_video_is_its_own_item() -> None:
    counts = count_items(
        [remote("/DCIM/100APPLE/IMG_0003.MOV"), remote("/DCIM/100APPLE/IMG_0004.MP4")]
    )
    assert counts.items == 2


def test_cpl_assets_count_as_items() -> None:
    counts = count_items(
        [
            remote("/DCIM/100APPLE/IMG_0001.HEIC"),
            remote("/DCIM/100APPLE/IMG_0001.MOV"),
            remote("/PhotoData/CPLAssets/group118/C3C25836-60EE-4C14-A10A-71BDDA8A0F31.JPG"),
        ]
    )
    assert counts.files == 3
    assert counts.items == 2


def test_matches_the_real_device_reconciliation() -> None:
    """16393 files = 16161 DCIM items + 220 Live Photo halves + 12 sidecars."""
    files = []
    for index in range(16161):
        files.append(remote(f"/DCIM/100APPLE/IMG_{index:05d}.JPG"))
    for index in range(220):
        files.append(remote(f"/DCIM/100APPLE/IMG_{index:05d}.MOV"))
    for index in range(220, 232):
        files.append(remote(f"/DCIM/100APPLE/IMG_{index:05d}.AAE"))

    counts = count_items(files)
    assert counts.files == 16393
    assert counts.items == 16161
    assert counts.extra_files == 232


def test_empty_listing() -> None:
    counts = count_items([])
    assert counts.files == 0
    assert counts.items == 0
    assert counts.bytes_total == 0


def test_a_live_photo_counts_as_a_photo_not_as_a_video() -> None:
    """The rule the split turns on, and the one a user would notice getting wrong.

    A Live Photo is a still and a ``.MOV`` half sharing a base name. The Photos app
    shows one photo; so does this. Counting the half as a video would have moved
    232 items into the wrong column on a real iPhone 13.
    """
    counts = count_items(
        [
            remote("/DCIM/100APPLE/IMG_0001.HEIC"),
            remote("/DCIM/100APPLE/IMG_0001.MOV"),
        ]
    )
    assert counts.items == 1
    assert counts.photos == 1
    assert counts.videos == 0


def test_a_video_on_its_own_is_a_video() -> None:
    counts = count_items([remote("/DCIM/100APPLE/IMG_0002.MOV")])
    assert (counts.photos, counts.videos, counts.other) == (0, 1, 0)


def test_an_edited_live_photo_is_still_one_photo() -> None:
    """Still + half + sidecar: three files, one item, one photo."""
    counts = count_items(
        [
            remote("/DCIM/100APPLE/IMG_0003.HEIC"),
            remote("/DCIM/100APPLE/IMG_0003.MOV"),
            remote("/DCIM/100APPLE/IMG_0003.AAE"),
        ]
    )
    assert counts.files == 3
    assert counts.items == 1
    assert (counts.photos, counts.videos, counts.other) == (1, 0, 0)


def test_an_edited_video_is_a_video() -> None:
    """A sidecar never decides what an item is; the content beside it does."""
    counts = count_items(
        [
            remote("/DCIM/100APPLE/IMG_0004.MOV"),
            remote("/DCIM/100APPLE/IMG_0004.AAE"),
        ]
    )
    assert (counts.photos, counts.videos, counts.other) == (0, 1, 0)


def test_the_split_adds_up_to_the_item_count() -> None:
    """Whatever the mix, every item lands in exactly one column."""
    counts = count_items(
        [
            remote("/DCIM/100APPLE/A.JPG"),
            remote("/DCIM/100APPLE/B.PNG"),
            remote("/DCIM/100APPLE/C.MP4"),
            remote("/DCIM/100APPLE/D.HEIC"),
            remote("/DCIM/100APPLE/D.MOV"),
            remote("/DCIM/100APPLE/E.DNG"),
            remote("/DCIM/100APPLE/F.OGG"),
        ]
    )
    assert counts.items == 6
    assert counts.photos + counts.videos + counts.other == counts.items
    assert (counts.photos, counts.videos, counts.other) == (4, 1, 1)


def test_an_extension_nobody_recognises_is_counted_apart() -> None:
    """Better an "other" column than a wrong one: iOS keeps adding formats."""
    counts = count_items([remote("/DCIM/100APPLE/G.SOMETHINGNEW")])
    assert (counts.photos, counts.videos, counts.other) == (0, 0, 1)
