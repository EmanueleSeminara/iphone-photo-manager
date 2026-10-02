"""Manifest behaviour: recording, verification, counters."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ipm.core.database import (
    SCHEMA_VERSION,
    ManifestDatabase,
    local_copy_is_intact,
    manifest_path_for,
)
from ipm.errors import IpmError
from ipm.models import ImportRecord, RemoteFile

STAMP = datetime(2024, 3, 14, 9, 0)


def _write(root: Path, relative: str, content: bytes = b"abc") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_manifest_is_created_inside_destination(destination: Path) -> None:
    db = ManifestDatabase(manifest_path_for(destination))
    try:
        assert (destination / ".ipm" / "manifest.db").exists()
    finally:
        db.close()


def test_record_and_get_roundtrip(database: ManifestDatabase) -> None:
    database.record_import(
        device_udid="UDID",
        device_path="/DCIM/100APPLE/IMG_0001.HEIC",
        size=3,
        mtime=1_700_000_000.0,
        local_path="2024/03/IMG_0001.HEIC",
    )
    record = database.get("UDID", "/DCIM/100APPLE/IMG_0001.HEIC")
    assert record is not None
    assert record.size == 3
    assert record.local_path == "2024/03/IMG_0001.HEIC"
    assert record.deleted_from_device_at is None


def test_record_import_is_idempotent_and_clears_deletion(database: ManifestDatabase) -> None:
    database.record_import(
        device_udid="UDID", device_path="/DCIM/A.HEIC", size=3, mtime=1.0, local_path="a.heic"
    )
    database.mark_deleted("UDID", ["/DCIM/A.HEIC"])
    assert database.get("UDID", "/DCIM/A.HEIC").deleted_from_device_at is not None

    database.record_import(
        device_udid="UDID", device_path="/DCIM/A.HEIC", size=5, mtime=2.0, local_path="b.heic"
    )
    record = database.get("UDID", "/DCIM/A.HEIC")
    assert record.size == 5
    assert record.local_path == "b.heic"
    assert record.deleted_from_device_at is None
    assert len(list(database.all_records())) == 1


def test_records_are_scoped_per_device(database: ManifestDatabase) -> None:
    database.record_import(
        device_udid="ONE", device_path="/DCIM/A.HEIC", size=1, mtime=1.0, local_path="one/a.heic"
    )
    database.record_import(
        device_udid="TWO", device_path="/DCIM/A.HEIC", size=1, mtime=1.0, local_path="two/a.heic"
    )
    assert len(database.records_for_device("ONE")) == 1
    assert len(database.records_for_device("TWO")) == 1
    assert database.known_local_paths() == {"one/a.heic", "two/a.heic"}


def test_live_records_exclude_deleted(database: ManifestDatabase) -> None:
    database.record_import(
        device_udid="UDID", device_path="/DCIM/A.HEIC", size=1, mtime=1.0, local_path="a.heic"
    )
    database.record_import(
        device_udid="UDID", device_path="/DCIM/B.HEIC", size=1, mtime=1.0, local_path="b.heic"
    )
    database.mark_deleted("UDID", ["/DCIM/B.HEIC"])
    assert [record.device_path for record in database.live_records("UDID")] == ["/DCIM/A.HEIC"]


def test_local_copy_is_intact_checks_size(destination: Path) -> None:
    _write(destination, "2024/03/IMG.HEIC", b"abc")
    record = ImportRecord(
        device_udid="UDID",
        device_path="/DCIM/IMG.HEIC",
        size=3,
        mtime=1.0,
        local_path="2024/03/IMG.HEIC",
        imported_at=datetime.now(UTC),
    )
    assert local_copy_is_intact(record, destination)

    (destination / "2024/03/IMG.HEIC").write_bytes(b"ab")
    assert not local_copy_is_intact(record, destination)

    (destination / "2024/03/IMG.HEIC").unlink()
    assert not local_copy_is_intact(record, destination)


def test_status_classifies_rows(database: ManifestDatabase, destination: Path) -> None:
    _write(destination, "2024/03/OK.HEIC", b"abc")
    database.record_import(
        device_udid="UDID", device_path="/DCIM/OK.HEIC", size=3, mtime=1.0,
        local_path="2024/03/OK.HEIC",
    )
    database.record_import(
        device_udid="UDID", device_path="/DCIM/GONE.HEIC", size=3, mtime=1.0,
        local_path="2024/03/GONE.HEIC",
    )
    database.record_import(
        device_udid="UDID", device_path="/DCIM/OLD.HEIC", size=3, mtime=1.0,
        local_path="2024/03/OLD.HEIC",
    )
    database.mark_deleted("UDID", ["/DCIM/OLD.HEIC"])

    status = database.status(destination, "UDID")
    assert (status.verified, status.unverified, status.deleted) == (1, 1, 1)
    assert status.verified_bytes == 3
    assert status.total == 3


def test_status_without_device_filter_counts_everything(
    database: ManifestDatabase, destination: Path
) -> None:
    _write(destination, "a.heic", b"abc")
    _write(destination, "b.heic", b"abc")
    database.record_import(
        device_udid="ONE", device_path="/DCIM/A.HEIC", size=3, mtime=1.0, local_path="a.heic"
    )
    database.record_import(
        device_udid="TWO", device_path="/DCIM/B.HEIC", size=3, mtime=1.0, local_path="b.heic"
    )
    assert database.status(destination).verified == 2
    assert database.status(destination, "ONE").verified == 1


def test_forget_removes_the_row(database: ManifestDatabase) -> None:
    database.record_import(
        device_udid="UDID", device_path="/DCIM/A.HEIC", size=1, mtime=1.0, local_path="a.heic"
    )
    database.forget("UDID", "/DCIM/A.HEIC")
    assert database.get("UDID", "/DCIM/A.HEIC") is None


def test_manifest_survives_reopen(destination: Path) -> None:
    path = manifest_path_for(destination)
    first = ManifestDatabase(path)
    first.record_import(
        device_udid="UDID", device_path="/DCIM/A.HEIC", size=1, mtime=1.0, local_path="a.heic"
    )
    first.close()

    second = ManifestDatabase(path)
    try:
        assert second.get("UDID", "/DCIM/A.HEIC") is not None
    finally:
        second.close()


def test_a_local_path_outside_the_folder_is_refused(database: ManifestDatabase) -> None:
    with pytest.raises(ValueError):
        database.record_import(
            device_udid="UDID",
            device_path="/DCIM/A.HEIC",
            size=1,
            mtime=1.0,
            local_path="../../elsewhere.HEIC",
        )


def test_a_tampered_row_never_counts_as_verified(
    database: ManifestDatabase, destination: Path
) -> None:
    """A hand-edited manifest must not make an unrelated file vouch for a photo."""
    outside = destination.parent / "unrelated.txt"
    outside.write_bytes(b"xyz")
    database._conn.execute(  # noqa: SLF001 - simulating a corrupted/edited manifest
        "INSERT INTO files (device_udid, device_path, size, mtime, local_path, imported_at) "
        "VALUES ('UDID', '/DCIM/A.HEIC', 3, 1.0, '../unrelated.txt', '2024-01-01T00:00:00')"
    )
    database._conn.commit()  # noqa: SLF001

    record = database.get("UDID", "/DCIM/A.HEIC")
    assert record is not None
    assert local_copy_is_intact(record, destination) is False
    assert database.status(destination).verified == 0


def test_digest_is_stored_and_read_back(database: ManifestDatabase) -> None:
    record = database.record_import(
        device_udid="UDID",
        device_path="/DCIM/A.HEIC",
        size=3,
        mtime=1.0,
        local_path="2024/03/A.HEIC",
        sha256="a" * 64,
    )
    assert record.sha256 == "a" * 64
    stored = database.get("UDID", "/DCIM/A.HEIC")
    assert stored is not None and stored.sha256 == "a" * 64


def test_a_digest_is_only_filled_in_when_missing(database: ManifestDatabase) -> None:
    database.record_import(
        device_udid="UDID", device_path="/DCIM/A.HEIC", size=3, mtime=1.0,
        local_path="a.heic", sha256="a" * 64,
    )
    database.record_import(
        device_udid="UDID", device_path="/DCIM/B.HEIC", size=3, mtime=1.0, local_path="b.heic",
    )

    database.record_digest("UDID", "/DCIM/A.HEIC", "b" * 64)
    database.record_digest("UDID", "/DCIM/B.HEIC", "c" * 64)

    kept = database.get("UDID", "/DCIM/A.HEIC")
    filled = database.get("UDID", "/DCIM/B.HEIC")
    assert kept is not None and kept.sha256 == "a" * 64  # the transfer's digest wins
    assert filled is not None and filled.sha256 == "c" * 64


def test_a_version_1_manifest_is_migrated_in_place(destination: Path) -> None:
    """A manifest at schema 1 must keep its history, not be recreated."""
    path = manifest_path_for(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy = sqlite3.connect(str(path))
    legacy.executescript(
        """
        CREATE TABLE schema_info (version INTEGER NOT NULL);
        INSERT INTO schema_info (version) VALUES (1);
        CREATE TABLE files (
            device_udid TEXT NOT NULL, device_path TEXT NOT NULL, size INTEGER NOT NULL,
            mtime REAL NOT NULL, local_path TEXT NOT NULL, imported_at TEXT NOT NULL,
            deleted_from_device_at TEXT, PRIMARY KEY (device_udid, device_path)
        );
        INSERT INTO files VALUES
            ('UDID', '/DCIM/OLD.HEIC', 3, 1.0, '2024/03/OLD.HEIC', '2026-08-01T10:00:00', NULL);
        """
    )
    legacy.commit()
    legacy.close()

    with ManifestDatabase(path) as database:
        record = database.get("UDID", "/DCIM/OLD.HEIC")
        assert record is not None
        assert record.local_path == "2024/03/OLD.HEIC"
        assert record.sha256 is None  # nothing is invented for an old row
        version = database._conn.execute("SELECT version FROM schema_info").fetchone()[0]
        assert version == SCHEMA_VERSION
        # The new device table came with the migration.
        database._conn.execute("SELECT device_udid FROM devices")


def test_a_manifest_from_the_future_is_refused(destination: Path) -> None:
    path = manifest_path_for(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    ahead = sqlite3.connect(str(path))
    ahead.execute("CREATE TABLE schema_info (version INTEGER NOT NULL)")
    ahead.execute("INSERT INTO schema_info (version) VALUES (?)", (SCHEMA_VERSION + 1,))
    ahead.commit()
    ahead.close()

    with pytest.raises(IpmError):
        ManifestDatabase(path)


def test_devices_are_remembered_with_their_first_sighting(database: ManifestDatabase) -> None:
    assert database.known_device("UDID") is None

    database.remember_device("UDID", name="iPhone di Test", product_type="iPhone14,5",
                             ios_version="18.5")
    first = database.known_device("UDID")
    assert first is not None
    assert (first.name, first.product_type, first.ios_version) == (
        "iPhone di Test", "iPhone14,5", "18.5",
    )

    database.remember_device("UDID", name="iPhone", product_type="iPhone14,5", ios_version="19.0")
    second = database.known_device("UDID")
    assert second is not None
    assert second.name == "iPhone"
    assert second.ios_version == "19.0"
    assert second.first_seen == first.first_seen  # the first sighting is never rewritten
    assert second.last_seen >= first.last_seen


async def test_status_counts_what_is_still_to_import(destination: Path, database) -> None:  # type: ignore[no-untyped-def]
    """The panel's "still to import" number, answered in the same pass as the rest."""
    (destination / "2024" / "03").mkdir(parents=True)
    (destination / "2024" / "03" / "A.HEIC").write_bytes(b"aaa")
    database.record_import(
        device_udid="UDID",
        device_path="/DCIM/A.HEIC",
        size=3,
        mtime=1.0,
        local_path="2024/03/A.HEIC",
    )

    on_phone = [
        RemoteFile(
            path="/DCIM/A.HEIC", size=3, created=STAMP, modified=STAMP
        ),
        RemoteFile(
            path="/DCIM/B.HEIC", size=40, created=STAMP, modified=STAMP
        ),
    ]
    status = database.status(destination, "UDID", on_phone)

    assert status.on_device_files == 2
    assert status.on_device_bytes == 43
    assert status.pending_files == 1
    assert status.pending_bytes == 40
    assert status.covered_files == 1
    assert status.is_complete is False


async def test_a_row_marked_deleted_does_not_count_as_coverage(
    destination: Path, database
) -> None:  # type: ignore[no-untyped-def]
    """A file deleted from the phone that came back is not "already imported"."""
    database.record_import(
        device_udid="UDID",
        device_path="/DCIM/A.HEIC",
        size=3,
        mtime=1.0,
        local_path="2024/03/A.HEIC",
    )
    database.mark_deleted("UDID", ["/DCIM/A.HEIC"])

    on_phone = [RemoteFile(path="/DCIM/A.HEIC", size=3, created=STAMP, modified=STAMP)]
    status = database.status(destination, "UDID", on_phone)

    assert status.deleted == 1
    assert status.pending_files == 1
    assert status.is_complete is False


async def test_status_says_nothing_about_a_phone_that_is_not_there(
    destination: Path, database
) -> None:  # type: ignore[no-untyped-def]
    status = database.status(destination, "UDID")
    assert status.on_device_files == 0
    assert status.pending_files == 0
    # Without a phone attached there is nothing to be complete about.
    assert status.is_complete is False


async def test_status_splits_the_verified_rows_into_photos_and_videos(
    destination: Path, database
) -> None:  # type: ignore[no-untyped-def]
    """A Live Photo is one photo: two files, one item, no video."""
    (destination / "2024" / "03").mkdir(parents=True)
    for name, content in (
        ("A.HEIC", b"aaa"),
        ("A.MOV", b"aaaaaa"),
        ("B.MP4", b"bbbb"),
    ):
        (destination / "2024" / "03" / name).write_bytes(content)
        database.record_import(
            device_udid="UDID",
            device_path=f"/DCIM/{name}",
            size=len(content),
            mtime=1.0,
            local_path=f"2024/03/{name}",
        )

    status = database.status(destination, "UDID")
    assert status.verified == 3
    assert status.verified_items == 2
    assert (status.photos, status.videos, status.other) == (1, 1, 0)


async def test_a_row_without_its_local_copy_is_in_no_column(
    destination: Path, database
) -> None:  # type: ignore[no-untyped-def]
    """The split describes what is in the folder, so a missing copy is not a photo."""
    database.record_import(
        device_udid="UDID",
        device_path="/DCIM/A.HEIC",
        size=3,
        mtime=1.0,
        local_path="2024/03/A.HEIC",
    )
    status = database.status(destination, "UDID")
    assert status.unverified == 1
    assert (status.photos, status.videos, status.other) == (0, 0, 0)
