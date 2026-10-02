"""Noticing that the phone stopped matching what the folder remembers."""

from __future__ import annotations

from datetime import UTC, datetime

from ipm.core.history import detect_changes
from ipm.models import ImportRecord, RemoteFile

MARCH = datetime(2024, 3, 14, 9, 0, 0)


def _record(path: str, size: int = 3, when: datetime = MARCH) -> ImportRecord:
    return ImportRecord(
        device_udid="UDID",
        device_path=path,
        size=size,
        mtime=when.timestamp(),
        local_path=f"2024/03/{path.rsplit('/', 1)[-1]}",
        imported_at=datetime.now(UTC),
    )


def _remote(path: str, size: int = 3, when: datetime = MARCH) -> RemoteFile:
    return RemoteFile(path=path, size=size, created=when, modified=when)


def test_an_unchanged_library_needs_no_attention() -> None:
    records = [_record("/DCIM/A.HEIC"), _record("/DCIM/B.HEIC")]
    change = detect_changes(records, [_remote("/DCIM/A.HEIC"), _remote("/DCIM/B.HEIC")])

    assert change.identical == 2
    assert change.needs_attention is False


def test_photos_deleted_on_the_phone_are_absent_not_alarming() -> None:
    records = [_record("/DCIM/A.HEIC"), _record("/DCIM/B.HEIC")]
    change = detect_changes(records, [_remote("/DCIM/A.HEIC")])

    assert [record.device_path for record in change.absent] == ["/DCIM/B.HEIC"]
    assert change.needs_attention is False


def test_a_reused_path_always_needs_attention() -> None:
    records = [_record("/DCIM/A.HEIC")]
    # Same name, same size, taken a year later: a different photo.
    change = detect_changes(records, [_remote("/DCIM/A.HEIC", when=datetime(2025, 9, 1))])

    assert [record.device_path for record in change.changed] == ["/DCIM/A.HEIC"]
    assert change.needs_attention is True


def test_a_wiped_phone_is_flagged_once_it_is_worth_flagging() -> None:
    many = [_record(f"/DCIM/IMG_{index:03d}.HEIC") for index in range(30)]
    assert detect_changes(many, []).everything_vanished is True

    few = [_record("/DCIM/A.HEIC"), _record("/DCIM/B.HEIC")]
    # Two files gone is a Tuesday, not a wipe.
    assert detect_changes(few, []).everything_vanished is False
    assert detect_changes(few, []).needs_attention is False


def test_counts_add_up() -> None:
    records = [_record("/DCIM/A.HEIC"), _record("/DCIM/B.HEIC"), _record("/DCIM/C.HEIC")]
    change = detect_changes(
        records,
        [_remote("/DCIM/A.HEIC"), _remote("/DCIM/B.HEIC", when=datetime(2025, 9, 1))],
    )

    assert (change.identical, len(change.changed), len(change.absent)) == (1, 1, 1)
    assert change.tracked == 3
