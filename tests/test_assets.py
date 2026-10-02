"""Matching imported files to the library items the phone knows about.

These rules decide which photo iOS is asked to delete. A wrong match deletes the
wrong picture and nothing gets it back, so the refusals matter as much as the
matches.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ipm.core.assets import match_assets
from ipm.models import DeviceAsset, ImportRecord


def _record(path: str, size: int = 10, mtime: float = 100.0) -> ImportRecord:
    return ImportRecord(
        device_udid="UDID",
        device_path=path,
        size=size,
        mtime=mtime,
        local_path=f"2024/03/{path.rsplit('/', 1)[-1]}",
        imported_at=datetime.now(UTC),
    )


def _asset(identifier: str, name: str, size: int = 10) -> DeviceAsset:
    return DeviceAsset(identifier=identifier, original_filename=name, size=size)


def test_a_still_matches_its_asset_by_name_and_size() -> None:
    records = [_record("/DCIM/100APPLE/IMG_0001.HEIC", 1234)]
    assets = [_asset("a1", "IMG_0001.HEIC", 1234)]

    plan = match_assets(records, assets)

    assert [target.asset.identifier for target in plan.targets] == ["a1"]
    assert plan.unmatched == []
    assert plan.total_bytes == 1234


def test_the_live_photo_half_rides_along_on_the_still() -> None:
    """The ``.MOV`` and the ``.AAE`` have no asset of their own -- and must not need one."""
    records = [
        _record("/DCIM/100APPLE/IMG_0001.HEIC", 1000),
        _record("/DCIM/100APPLE/IMG_0001.MOV", 4000),
        _record("/DCIM/100APPLE/IMG_0001.AAE", 300),
    ]
    assets = [_asset("a1", "IMG_0001.HEIC", 1000)]

    plan = match_assets(records, assets)

    assert len(plan.targets) == 1
    assert [record.device_path for record in plan.records] == [
        "/DCIM/100APPLE/IMG_0001.AAE",
        "/DCIM/100APPLE/IMG_0001.HEIC",
        "/DCIM/100APPLE/IMG_0001.MOV",
    ]
    assert plan.total_bytes == 5300
    assert plan.refused == 0


def test_a_standalone_video_is_an_item_of_its_own() -> None:
    records = [_record("/DCIM/100APPLE/IMG_0002.MOV", 9000)]
    assets = [_asset("a2", "IMG_0002.MOV", 9000)]

    plan = match_assets(records, assets)

    assert len(plan.targets) == 1


def test_a_size_that_does_not_agree_is_not_a_match() -> None:
    """Same name, different size: a restored phone renumbering from IMG_0001 again."""
    records = [_record("/DCIM/100APPLE/IMG_0001.HEIC", 1000)]
    assets = [_asset("a1", "IMG_0001.HEIC", 2000)]

    plan = match_assets(records, assets)

    assert plan.targets == []
    assert [group.key for group in plan.unmatched] == ["/DCIM/100APPLE/IMG_0001"]


def test_a_file_with_no_item_is_refused_rather_than_unlinked() -> None:
    records = [_record("/DCIM/100APPLE/IMG_0009.AAE", 300)]

    plan = match_assets(records, [])

    assert plan.targets == []
    assert plan.refused == 1
    assert plan.records == []


def test_two_assets_claiming_one_group_are_refused() -> None:
    """Never seen on hardware; deleting the wrong photo is unrecoverable anyway."""
    records = [
        _record("/DCIM/100APPLE/IMG_0001.HEIC", 1000),
        _record("/DCIM/100APPLE/IMG_0001.MOV", 4000),
    ]
    assets = [_asset("a1", "IMG_0001.HEIC", 1000), _asset("a2", "IMG_0001.MOV", 4000)]

    plan = match_assets(records, assets)

    assert plan.targets == []
    assert [group.key for group in plan.ambiguous] == ["/DCIM/100APPLE/IMG_0001"]


def test_one_asset_claimed_by_two_groups_takes_neither() -> None:
    """Deleting one would silently take the other with it."""
    records = [
        _record("/DCIM/100APPLE/IMG_0001.HEIC", 1000),
        _record("/DCIM/101APPLE/IMG_0001.HEIC", 1000),
    ]
    assets = [_asset("a1", "IMG_0001.HEIC", 1000)]

    plan = match_assets(records, assets)

    assert plan.targets == []
    assert len(plan.ambiguous) == 2
    assert plan.records == []


def test_items_with_no_imported_file_are_simply_ignored() -> None:
    """The rendered versions of edited photos live on the phone and are never imported."""
    records = [_record("/DCIM/100APPLE/IMG_0001.HEIC", 1000)]
    assets = [
        _asset("a1", "IMG_0001.HEIC", 1000),
        _asset("a2", "FullSizeRender.HEIC", 2000),
    ]

    plan = match_assets(records, assets)

    assert [target.asset.identifier for target in plan.targets] == ["a1"]


def test_targets_come_out_newest_first() -> None:
    records = [
        _record("/DCIM/100APPLE/IMG_0001.HEIC", 10, mtime=100.0),
        _record("/DCIM/100APPLE/IMG_0003.HEIC", 10, mtime=300.0),
        _record("/DCIM/100APPLE/IMG_0002.HEIC", 10, mtime=200.0),
    ]
    assets = [_asset(f"a{n}", f"IMG_000{n}.HEIC", 10) for n in (1, 2, 3)]

    plan = match_assets(records, assets)

    assert [target.group.name for target in plan.targets] == [
        "IMG_0003.HEIC",
        "IMG_0002.HEIC",
        "IMG_0001.HEIC",
    ]
