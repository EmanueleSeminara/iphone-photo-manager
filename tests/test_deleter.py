"""Deletion safety rules, and the choice of what to delete."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path

from ipm.core.assets import match_assets
from ipm.core.database import ManifestDatabase
from ipm.core.deleter import (
    Deleter,
    _why_it_has_no_item,
    collect_candidates,
    group_by_item,
    select_newest_items,
)
from ipm.core.importer import Importer
from ipm.models import DeviceAsset, ImportRecord
from tests.fakes import FakeAssetService, FakeBackend, make_file

MARCH = datetime(2024, 3, 14, 9, 0, 0)


def _record(path: str, mtime: float, size: int = 10) -> ImportRecord:
    """A manifest row with just enough filled in for the selection rules."""
    return ImportRecord(
        device_udid="UDID",
        device_path=path,
        size=size,
        mtime=mtime,
        local_path=f"2024/03/{path.rsplit('/', 1)[-1]}",
        imported_at=datetime.now(UTC),
    )


async def _import(backend: FakeBackend, database: ManifestDatabase, destination: Path) -> None:
    await Importer(backend, database, destination).run()


def _deleter(
    backend: FakeBackend,
    database: ManifestDatabase,
    destination: Path,
    assets: FakeAssetService | None = None,
    **kwargs: object,
) -> Deleter:
    """A deleter wired to the fake photo library that owns *backend*'s files."""
    return Deleter(
        backend,
        database,
        destination,
        assets=assets or FakeAssetService(backend),
        **kwargs,  # type: ignore[arg-type]
    )


async def test_only_verified_files_are_deletable(
    database: ManifestDatabase, destination: Path
) -> None:
    backend = FakeBackend.with_files(
        make_file("/DCIM/A.HEIC", b"aaa", MARCH),
        make_file("/DCIM/B.HEIC", b"bbb", MARCH),
    )
    await _import(backend, database, destination)
    (destination / "2024/03/B.HEIC").unlink()

    candidates = collect_candidates(database, destination, backend.serial)
    assert [record.device_path for record in candidates.deletable] == ["/DCIM/A.HEIC"]
    assert [record.device_path for record in candidates.unverified] == ["/DCIM/B.HEIC"]
    assert candidates.total_bytes == 3


async def test_truncated_local_copy_is_never_deletable(
    database: ManifestDatabase, destination: Path
) -> None:
    backend = FakeBackend.with_files(make_file("/DCIM/A.HEIC", b"aaaaaa", MARCH))
    await _import(backend, database, destination)
    (destination / "2024/03/A.HEIC").write_bytes(b"aa")

    candidates = collect_candidates(database, destination, backend.serial)
    assert candidates.deletable == []
    assert len(candidates.unverified) == 1


async def test_delete_removes_from_device_and_records_it(
    database: ManifestDatabase, destination: Path
) -> None:
    backend = FakeBackend.with_files(
        make_file("/DCIM/A.HEIC", b"aaa", MARCH),
        make_file("/DCIM/B.HEIC", b"bbbb", MARCH),
    )
    await _import(backend, database, destination)

    stats = await _deleter(backend, database, destination).run()

    assert stats.deleted == 2
    assert stats.bytes_freed == 7
    assert sorted(backend.deletions) == ["/DCIM/A.HEIC", "/DCIM/B.HEIC"]
    assert backend.files == {}
    assert database.live_records(backend.serial) == []
    # The local copies are untouched.
    assert (destination / "2024/03/A.HEIC").read_bytes() == b"aaa"


async def test_delete_never_touches_unverified_files(
    database: ManifestDatabase, destination: Path
) -> None:
    backend = FakeBackend.with_files(
        make_file("/DCIM/A.HEIC", b"aaa", MARCH),
        make_file("/DCIM/B.HEIC", b"bbb", MARCH),
    )
    await _import(backend, database, destination)
    (destination / "2024/03/B.HEIC").unlink()

    stats = await _deleter(backend, database, destination).run()

    assert stats.deleted == 1
    assert backend.deletions == ["/DCIM/A.HEIC"]
    assert "/DCIM/B.HEIC" in backend.files


async def test_copy_removed_after_confirmation_is_skipped(
    database: ManifestDatabase, destination: Path
) -> None:
    """The verification is repeated per file, not trusted from the dialog."""
    backend = FakeBackend.with_files(
        make_file("/DCIM/A.HEIC", b"aaa", MARCH),
        make_file("/DCIM/B.HEIC", b"bbb", MARCH),
    )
    await _import(backend, database, destination)
    deleter = _deleter(backend, database, destination)
    candidates = await deleter.candidates()
    assert len(candidates.deletable) == 2

    # The user deletes a local copy between the dialog and the run.
    (destination / "2024/03/B.HEIC").unlink()

    stats = await deleter.run(candidates.deletable)
    assert stats.deleted == 1
    assert stats.failed == 1
    assert "/DCIM/B.HEIC" in backend.files


async def test_an_item_the_phone_refuses_to_delete_is_reported_not_lost(
    database: ManifestDatabase, destination: Path
) -> None:
    """A request the phone ignores must not become a manifest row claiming success."""
    backend = FakeBackend.with_files(
        make_file("/DCIM/A.HEIC", b"aaa", MARCH),
        make_file("/DCIM/B.HEIC", b"bbb", MARCH),
    )
    await _import(backend, database, destination)
    assets = FakeAssetService(backend, fail_identifiers={"/DCIM/B.HEIC"})

    stats = await _deleter(backend, database, destination, assets).run()

    assert stats.deleted == 1
    assert stats.failed == 1
    assert stats.leftover == ["/DCIM/B.HEIC"]
    assert "/DCIM/B.HEIC" in backend.files
    # The one that stayed is still marked as present, so it can be retried.
    live = [record.device_path for record in database.live_records(backend.serial)]
    assert live == ["/DCIM/B.HEIC"]


async def test_a_live_photo_half_left_behind_is_reported(
    database: ManifestDatabase, destination: Path
) -> None:
    """The one behaviour nobody has been able to observe on hardware yet.

    Deleting a library item is expected to take the Live Photo's ``.MOV`` half and the
    ``.AAE`` sidecar with it, since neither has an item of its own. If a future iOS
    does not, the files must show up as left behind rather than be quietly assumed
    gone -- and their manifest rows must stay live, so a later run tries again.
    """
    backend = FakeBackend.with_files(
        make_file("/DCIM/IMG_1.HEIC", b"aaa", MARCH),
        make_file("/DCIM/IMG_1.MOV", b"mmmm", MARCH),
        make_file("/DCIM/IMG_1.AAE", b"ee", MARCH),
    )
    await _import(backend, database, destination)
    assets = FakeAssetService(backend, leave_related=True)

    stats = await _deleter(backend, database, destination, assets).run()

    assert stats.deleted == 1
    assert stats.items_deleted == 0  # the item is not fully gone
    assert stats.leftover == ["/DCIM/IMG_1.AAE", "/DCIM/IMG_1.MOV"]
    live = sorted(record.device_path for record in database.live_records(backend.serial))
    assert live == ["/DCIM/IMG_1.AAE", "/DCIM/IMG_1.MOV"]


async def test_a_whole_live_photo_goes_on_one_request(
    database: ManifestDatabase, destination: Path
) -> None:
    """The expected behaviour: one item deleted, all three files gone."""
    backend = FakeBackend.with_files(
        make_file("/DCIM/IMG_1.HEIC", b"aaa", MARCH),
        make_file("/DCIM/IMG_1.MOV", b"mmmm", MARCH),
        make_file("/DCIM/IMG_1.AAE", b"ee", MARCH),
    )
    await _import(backend, database, destination)
    assets = FakeAssetService(backend)

    stats = await _deleter(backend, database, destination, assets).run()

    assert assets.requested == ["/DCIM/IMG_1.HEIC"]  # one request, not three
    assert stats.items_deleted == 1
    assert stats.deleted == 3
    assert stats.bytes_freed == 9
    assert backend.files == {}
    assert database.live_records(backend.serial) == []


async def test_a_file_with_no_library_item_is_kept(
    database: ManifestDatabase, destination: Path
) -> None:
    """No item means no way to delete it cleanly, so it stays on the phone.

    Removing the file anyway is exactly the bug that moved deletion off the file
    system: the library row would survive and the Photos app would show an item it
    can no longer load.
    """
    backend = FakeBackend.with_files(make_file("/DCIM/IMG_1.AAE", b"ee", MARCH))
    await _import(backend, database, destination)

    stats = await _deleter(backend, database, destination).run()

    assert stats.deleted == 0
    assert stats.items_refused == 1
    assert stats.errors and "IMG_1.AAE" in stats.errors[0]
    assert "/DCIM/IMG_1.AAE" in backend.files
    assert len(database.live_records(backend.serial)) == 1


async def test_cancellation_records_what_was_already_deleted(
    database: ManifestDatabase, destination: Path
) -> None:
    files = [make_file(f"/DCIM/IMG_{index:02d}.HEIC", b"x" * 10, MARCH) for index in range(10)]
    backend = FakeBackend.with_files(*files)
    await _import(backend, database, destination)

    cancel = asyncio.Event()
    assets = FakeAssetService(backend)
    original = assets._remove_item  # noqa: SLF001 - simulating the cable being pulled

    def cancelling(identifier: str) -> None:
        original(identifier)
        if len(backend.deletions) >= 3:
            cancel.set()

    assets._remove_item = cancelling  # type: ignore[method-assign]  # noqa: SLF001

    stats = await _deleter(backend, database, destination, assets, cancel=cancel).run()

    assert stats.cancelled is True
    assert 0 < stats.deleted < 10
    # Items never requested are not failures, and the manifest still lists them.
    assert stats.leftover == []
    assert len(database.live_records(backend.serial)) == 10 - stats.deleted
    assert len(backend.files) == 10 - stats.deleted


def test_live_photo_halves_and_sidecars_form_one_item() -> None:
    records = [
        _record("/DCIM/100APPLE/IMG_0001.HEIC", 100.0),
        _record("/DCIM/100APPLE/IMG_0001.MOV", 100.0),
        _record("/DCIM/100APPLE/IMG_0001.AAE", 100.0),
        _record("/DCIM/100APPLE/IMG_0002.MOV", 200.0),
    ]
    groups = group_by_item(records)

    assert [group.key for group in groups] == [
        "/DCIM/100APPLE/IMG_0002",
        "/DCIM/100APPLE/IMG_0001",
    ]
    assert len(groups[1].records) == 3
    assert groups[1].size == 30


def test_a_partial_selection_takes_whole_items_newest_first() -> None:
    records = [
        _record("/DCIM/100APPLE/IMG_0001.HEIC", 100.0),
        _record("/DCIM/100APPLE/IMG_0002.HEIC", 200.0),
        _record("/DCIM/100APPLE/IMG_0002.MOV", 201.0),
        _record("/DCIM/100APPLE/IMG_0003.HEIC", 300.0),
    ]

    selected = select_newest_items(records, 2)

    # The two newest items are 0003 and 0002 -- and 0002 brings its Live Photo half.
    assert [record.device_path for record in selected] == [
        "/DCIM/100APPLE/IMG_0002.HEIC",
        "/DCIM/100APPLE/IMG_0002.MOV",
        "/DCIM/100APPLE/IMG_0003.HEIC",
    ]


def test_a_live_photo_is_dated_by_its_video_half_not_its_still() -> None:
    """The bug of 2026-09-04: "delete the newest item" deleted the oldest of a burst.

    These timestamps are copied from a real iPhone 13. Three Live Photos were taken
    four seconds apart, ``IMG_7128`` first; iOS wrote the video halves in that order
    and the stills in the opposite one, twenty seconds later. Dating each item by its
    newest file made the first photo of the burst look like the most recent item on
    the phone.
    """
    records = [
        _record("/DCIM/117APPLE/IMG_7128.MOV", 1788_57_745.698),
        _record("/DCIM/117APPLE/IMG_7128.HEIC", 1788_57_769.331),
        _record("/DCIM/117APPLE/IMG_7129.MOV", 1788_57_749.807),
        _record("/DCIM/117APPLE/IMG_7129.HEIC", 1788_57_768.292),
        _record("/DCIM/117APPLE/IMG_7130.MOV", 1788_57_753.581),
        _record("/DCIM/117APPLE/IMG_7130.HEIC", 1788_57_767.336),
    ]

    groups = group_by_item(records)

    assert [group.key for group in groups] == [
        "/DCIM/117APPLE/IMG_7130",
        "/DCIM/117APPLE/IMG_7129",
        "/DCIM/117APPLE/IMG_7128",
    ]
    # And the whole point: asking for one item gets the photo taken last, with its half.
    assert [record.device_path for record in select_newest_items(records, 1)] == [
        "/DCIM/117APPLE/IMG_7130.HEIC",
        "/DCIM/117APPLE/IMG_7130.MOV",
    ]


def test_an_edited_old_photo_does_not_become_the_newest_item() -> None:
    """An ``.AAE`` sidecar is written when the edit is made, not when the photo was
    taken, so it must not drag an old item to the top of the list."""
    records = [
        _record("/DCIM/100APPLE/IMG_0001.HEIC", 100.0),
        _record("/DCIM/100APPLE/IMG_0001.AAE", 900.0),
        _record("/DCIM/100APPLE/IMG_0002.HEIC", 200.0),
    ]

    assert [group.key for group in group_by_item(records)] == [
        "/DCIM/100APPLE/IMG_0002",
        "/DCIM/100APPLE/IMG_0001",
    ]


def test_items_stamped_the_same_second_fall_back_to_the_camera_numbering() -> None:
    """iOS numbers photos in the order they are taken, so on an exact tie the higher
    number is the more recent photo."""
    records = [
        _record("/DCIM/117APPLE/IMG_7001.JPG", 500.0),
        _record("/DCIM/117APPLE/IMG_7002.JPG", 500.0),
        _record("/DCIM/117APPLE/IMG_7003.JPG", 500.0),
    ]

    assert [group.key for group in group_by_item(records)] == [
        "/DCIM/117APPLE/IMG_7003",
        "/DCIM/117APPLE/IMG_7002",
        "/DCIM/117APPLE/IMG_7001",
    ]


def test_a_kept_photo_says_whether_the_library_almost_had_it() -> None:
    """The sentence that was missing on 2026-09-05.

    "The photo library has no item for it" was true and useless: it could not tell a
    photo iOS has never heard of from one whose size no longer agrees with the
    library, and answering that took a session with the phone attached and a dumped
    catalogue. Now the message carries the evidence.
    """
    row = _record("/DCIM/117APPLE/IMG_7131.HEIC", 100.0, size=2358253)
    library = [
        DeviceAsset(identifier="1", original_filename="IMG_7131.HEIC", size=2425738),
    ]

    plan = match_assets([row], library)
    assert not plan.targets
    assert len(plan.unmatched) == 1

    message = _why_it_has_no_item(plan.unmatched[0], plan.near_misses[plan.unmatched[0].key])
    assert "does hold an item called IMG_7131.HEIC" in message
    assert "2.31 MB" in message and "2.25 MB" in message


def test_a_photo_the_library_never_heard_of_says_so_instead() -> None:
    row = _record("/DCIM/117APPLE/IMG_9999.HEIC", 100.0, size=10)

    plan = match_assets([row], [])

    message = _why_it_has_no_item(plan.unmatched[0], plan.near_misses[plan.unmatched[0].key])
    assert "has no item for it" in message
    assert "rather than" not in message


async def test_a_phone_that_refuses_says_so_and_still_gets_re_scanned(
    tmp_path: Path,
) -> None:
    """Two real deletions removed nothing and could not be explained.

    iOS answers ``requestDeleteFiles`` through a callback that carries an error, and
    that error used to go nowhere: the run reported "removed 0 item(s), kept 2" and
    the reason was discarded inside the worker. Now it reaches the user -- and the
    confirming re-scan still runs, because a manifest that recorded a deletion which
    never happened would be worse than the silence.
    """
    destination = tmp_path / "photos"
    destination.mkdir()
    backend = FakeBackend.with_files(
        make_file("/DCIM/A.HEIC", b"aaa", datetime(2024, 3, 1, 9, 0)),
    )
    database = ManifestDatabase(destination / ".ipm" / "manifest.db")
    try:
        media = await backend.list_media()
        await Importer(backend, database, destination).run()
        assets = FakeAssetService(backend, refusal="The iPhone said: no.")
        deleter = Deleter(backend, database, destination, assets=assets, device_files=media)

        stats = await deleter.run(list(database.live_records(backend.serial)))

        assert stats.items_deleted == 0
        assert any("The iPhone said: no." in message for message in stats.errors)
        # The photo is still on the phone, and the manifest still says so.
        assert "/DCIM/A.HEIC" in backend.files
        assert [row.device_path for row in database.live_records(backend.serial)] == [
            "/DCIM/A.HEIC"
        ]
    finally:
        database.close()


async def test_a_request_the_phone_ignores_is_sent_once_more(tmp_path: Path) -> None:
    """The iPhone sometimes accepts a deletion, says nothing is wrong, and does
    nothing. Sending the same request again works -- found by hand, now automatic."""
    destination = tmp_path / "photos"
    destination.mkdir()
    backend = FakeBackend.with_files(
        make_file("/DCIM/A.HEIC", b"aaa", datetime(2024, 3, 1, 9, 0)),
    )
    database = ManifestDatabase(destination / ".ipm" / "manifest.db")
    try:
        media = await backend.list_media()
        await Importer(backend, database, destination).run()
        assets = FakeAssetService(backend, ignore_first_request=True)
        deleter = Deleter(backend, database, destination, assets=assets, device_files=media)

        stats = await deleter.run(list(database.live_records(backend.serial)))

        assert stats.retried is True
        assert stats.items_deleted == 1
        assert stats.failed == 0
        assert stats.leftover == []
        assert "/DCIM/A.HEIC" not in backend.files
        assert database.live_records(backend.serial) == []
    finally:
        database.close()


async def test_an_item_the_library_cannot_match_does_not_block_the_retry(
    tmp_path: Path,
) -> None:
    """A kept item is our decision, not the phone arguing back.

    The retry used to be gated on ``stats.errors`` being empty. By the time it is
    consulted that list also holds every row the gate declined and every group the
    plan refused -- and a real library always has some: 60 of 16 749 on the iPhone
    this was measured against. So the one situation the retry exists for, a whole
    library the phone silently declines to touch, was the one situation in which it
    could never run.
    """
    destination = tmp_path / "photos"
    destination.mkdir()
    backend = FakeBackend.with_files(
        make_file("/DCIM/A.HEIC", b"aaa", datetime(2024, 3, 1, 9, 0)),
        make_file("/DCIM/B.HEIC", b"bbbb", datetime(2024, 3, 2, 9, 0)),
    )
    database = ManifestDatabase(destination / ".ipm" / "manifest.db")
    try:
        media = await backend.list_media()
        await Importer(backend, database, destination).run()
        # B is on the phone but its photo library has no item for it, so the plan
        # keeps it and says why -- one line in stats.errors, about a file that is
        # not in the request at all.
        assets = FakeAssetService(
            backend, hidden={"/DCIM/B.HEIC"}, ignore_first_request=True
        )
        deleter = Deleter(backend, database, destination, assets=assets, device_files=media)

        stats = await deleter.run(list(database.live_records(backend.serial)))

        assert stats.retried is True
        assert stats.items_deleted == 1
        assert "/DCIM/A.HEIC" not in backend.files
        # B was never requested, and is untouched and explained.
        assert "/DCIM/B.HEIC" in backend.files
        assert stats.items_refused == 1
        assert any("B.HEIC" in message for message in stats.errors)
    finally:
        database.close()


async def test_a_phone_that_answers_with_an_error_is_not_asked_again(
    tmp_path: Path,
) -> None:
    """A refusal is information. Repeating a request the phone argued with is not
    the answer, and that is the half of the old rule worth keeping."""
    destination = tmp_path / "photos"
    destination.mkdir()
    backend = FakeBackend.with_files(
        make_file("/DCIM/A.HEIC", b"aaa", datetime(2024, 3, 1, 9, 0)),
    )
    database = ManifestDatabase(destination / ".ipm" / "manifest.db")
    try:
        media = await backend.list_media()
        await Importer(backend, database, destination).run()
        assets = FakeAssetService(backend, refusal="The iPhone said: no.")
        deleter = Deleter(backend, database, destination, assets=assets, device_files=media)

        stats = await deleter.run(list(database.live_records(backend.serial)))

        assert stats.retried is False
        assert assets.requested == []
        assert "/DCIM/A.HEIC" in backend.files
    finally:
        database.close()


async def test_a_partial_deletion_is_not_retried(tmp_path: Path) -> None:
    """Only a run where *nothing* went is retried.

    A leftover after a real deletion is a different question -- a Live Photo half
    that did not ride along -- and asking again would not answer it.
    """
    destination = tmp_path / "photos"
    destination.mkdir()
    backend = FakeBackend.with_files(
        make_file("/DCIM/A.HEIC", b"aaa", datetime(2024, 3, 1, 9, 0)),
        make_file("/DCIM/A.MOV", b"aaaa", datetime(2024, 3, 1, 9, 0)),
    )
    database = ManifestDatabase(destination / ".ipm" / "manifest.db")
    try:
        media = await backend.list_media()
        await Importer(backend, database, destination).run()
        assets = FakeAssetService(backend, leave_related=True)
        deleter = Deleter(backend, database, destination, assets=assets, device_files=media)

        stats = await deleter.run(list(database.live_records(backend.serial)))

        assert stats.retried is False
        assert stats.leftover == ["/DCIM/A.MOV"]
        assert assets.requested == ["/DCIM/A.HEIC"]
    finally:
        database.close()


def test_selection_limits_are_clamped_at_both_ends() -> None:
    records = [_record(f"/DCIM/IMG_{index}.HEIC", float(index)) for index in range(3)]

    assert select_newest_items(records, None) == sorted(records, key=lambda r: r.device_path)
    assert len(select_newest_items(records, 99)) == 3
    assert select_newest_items(records, 0) == []
    assert select_newest_items(records, -5) == []


async def test_deleting_a_subset_leaves_the_rest_on_the_phone(
    database: ManifestDatabase, destination: Path
) -> None:
    """The whole point of the partial delete: a 1-item trial run."""
    backend = FakeBackend.with_files(
        make_file("/DCIM/A.HEIC", b"aaa", datetime(2024, 3, 1, 9, 0)),
        make_file("/DCIM/B.HEIC", b"bbb", datetime(2024, 3, 2, 9, 0)),
        make_file("/DCIM/C.HEIC", b"ccc", datetime(2024, 3, 3, 9, 0)),
    )
    await _import(backend, database, destination)
    deleter = _deleter(backend, database, destination)
    candidates = await deleter.candidates()

    stats = await deleter.run(select_newest_items(candidates.deletable, 1))

    assert stats.deleted == 1
    assert backend.deletions == ["/DCIM/C.HEIC"]
    assert sorted(backend.files) == ["/DCIM/A.HEIC", "/DCIM/B.HEIC"]
    live = sorted(record.device_path for record in database.live_records(backend.serial))
    assert live == ["/DCIM/A.HEIC", "/DCIM/B.HEIC"]


async def test_delete_is_never_triggered_by_an_import(
    database: ManifestDatabase, destination: Path
) -> None:
    backend = FakeBackend.with_files(make_file("/DCIM/A.HEIC", b"aaa", MARCH))
    await _import(backend, database, destination)
    assert backend.deletions == []
    assert "/DCIM/A.HEIC" in backend.files


async def test_a_reused_device_path_is_never_deleted(
    database: ManifestDatabase, destination: Path
) -> None:
    """After an erase-and-restore the phone renumbers from IMG_0001 again.

    The UDID does not change, so the old manifest row finds a brand new photo at its
    device path. Deleting it on the strength of the old local copy would destroy a
    picture that was never imported.
    """
    backend = FakeBackend.with_files(make_file("/DCIM/A.HEIC", b"aaa", MARCH))
    await _import(backend, database, destination)

    # Same path, same size, different photo (different capture time).
    backend.files["/DCIM/A.HEIC"] = make_file("/DCIM/A.HEIC", b"zzz", datetime(2025, 9, 1, 8, 0))
    listing = await backend.list_media()

    candidates = collect_candidates(database, destination, backend.serial, listing)
    assert candidates.deletable == []
    assert [record.device_path for record in candidates.changed_on_device] == ["/DCIM/A.HEIC"]

    # And even if something hands the run that record anyway, the file survives.
    stale = database.live_records(backend.serial)
    stats = await _deleter(backend, database, destination, device_files=listing).run(stale)
    assert stats.deleted == 0
    assert stats.failed == 1
    assert backend.files["/DCIM/A.HEIC"].content == b"zzz"


async def test_a_reused_device_path_is_imported_as_a_new_file(
    database: ManifestDatabase, destination: Path
) -> None:
    """The other half of the same story: the new photo must still arrive."""
    backend = FakeBackend.with_files(make_file("/DCIM/A.HEIC", b"aaa", MARCH))
    await _import(backend, database, destination)

    backend.files["/DCIM/A.HEIC"] = make_file("/DCIM/A.HEIC", b"zzz", datetime(2025, 9, 1, 8, 0))
    await _import(backend, database, destination)

    assert (destination / "2024/03/A.HEIC").read_bytes() == b"aaa"
    assert (destination / "2025/09/A.HEIC").read_bytes() == b"zzz"
    record = database.get(backend.serial, "/DCIM/A.HEIC")
    assert record is not None and record.local_path == "2025/09/A.HEIC"


async def test_files_gone_from_the_phone_are_reported_not_deleted(
    database: ManifestDatabase, destination: Path
) -> None:
    backend = FakeBackend.with_files(
        make_file("/DCIM/A.HEIC", b"aaa", MARCH),
        make_file("/DCIM/B.HEIC", b"bbb", MARCH),
    )
    await _import(backend, database, destination)
    del backend.files["/DCIM/B.HEIC"]  # deleted on the phone, in the Photos app

    candidates = collect_candidates(database, destination, backend.serial, await backend.list_media())
    assert [record.device_path for record in candidates.deletable] == ["/DCIM/A.HEIC"]
    assert [record.device_path for record in candidates.absent_from_device] == ["/DCIM/B.HEIC"]


async def test_deep_check_catches_a_local_copy_that_changed_behind_our_back(
    database: ManifestDatabase, destination: Path
) -> None:
    """Same size, different bytes: only the digest can tell, and it must."""
    backend = FakeBackend.with_files(
        make_file("/DCIM/A.HEIC", b"aaa", MARCH),
        make_file("/DCIM/B.HEIC", b"bbb", MARCH),
    )
    await _import(backend, database, destination)
    (destination / "2024/03/B.HEIC").write_bytes(b"xxx")  # corrupted, still 3 bytes

    deleter = _deleter(backend, database, destination)
    candidates = await deleter.candidates()
    assert len(candidates.deletable) == 2  # the size check is happy with both

    result = await deleter.verify(candidates.deletable)
    assert [record.device_path for record in result.verified] == ["/DCIM/A.HEIC"]
    assert [record.device_path for record in result.mismatched] == ["/DCIM/B.HEIC"]
    assert [record.device_path for record in result.safe] == ["/DCIM/A.HEIC"]

    stats = await deleter.run(result.safe)
    assert stats.deleted == 1
    assert "/DCIM/B.HEIC" in backend.files


async def test_deep_check_reports_a_copy_it_cannot_read(
    database: ManifestDatabase, destination: Path
) -> None:
    backend = FakeBackend.with_files(make_file("/DCIM/A.HEIC", b"aaa", MARCH))
    await _import(backend, database, destination)
    (destination / "2024/03/A.HEIC").unlink()

    result = await _deleter(backend, database, destination).verify(
        database.live_records(backend.serial)
    )
    assert len(result.unreadable) == 1
    assert result.safe == []


async def test_deep_check_fills_in_a_missing_digest(
    database: ManifestDatabase, destination: Path
) -> None:
    """Rows imported before checksums existed pass on size, and gain a digest."""
    backend = FakeBackend.with_files(make_file("/DCIM/A.HEIC", b"aaa", MARCH))
    await _import(backend, database, destination)
    database._conn.execute("UPDATE files SET sha256 = NULL")  # noqa: SLF001 - a row
    database._conn.commit()  # noqa: SLF001

    result = await _deleter(backend, database, destination).verify(
        database.live_records(backend.serial)
    )

    assert [record.device_path for record in result.unhashed] == ["/DCIM/A.HEIC"]
    assert result.safe  # size-only, but still deletable
    stored = database.get(backend.serial, "/DCIM/A.HEIC")
    assert stored is not None and stored.sha256 == hashlib.sha256(b"aaa").hexdigest()


async def test_deep_check_stops_when_cancelled(
    database: ManifestDatabase, destination: Path
) -> None:
    files = [make_file(f"/DCIM/IMG_{index:02d}.HEIC", b"x" * 4096, MARCH) for index in range(20)]
    backend = FakeBackend.with_files(*files)
    await _import(backend, database, destination)

    cancel = asyncio.Event()
    cancel.set()
    result = await _deleter(backend, database, destination, cancel=cancel).verify(
        database.live_records(backend.serial)
    )

    assert result.cancelled is True
    assert result.safe == []
