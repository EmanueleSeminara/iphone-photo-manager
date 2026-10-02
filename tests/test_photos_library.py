"""Reading ZASSET and reconciling it with a device listing.

Built on synthetic databases with the same shape as the real one (verified on an
iPhone 13: ZDIRECTORY without a leading slash, ZTRASHEDSTATE / ZHIDDEN /
ZVISIBILITYSTATE all zero for an item the Photos app counts).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from ipm.core.photos_library import read_snapshot, reconcile
from ipm.errors import IpmError
from ipm.models import RemoteFile

STAMP = datetime(2024, 3, 14)


def remote(path: str, size: int = 10) -> RemoteFile:
    return RemoteFile(path=path, size=size, created=STAMP, modified=STAMP)


def make_db(tmp_path: Path, rows: list[tuple[str, str, int, int, int]]) -> Path:
    """Create a database with ``(directory, filename, trashed, hidden, visibility)``."""
    path = tmp_path / "Photos.sqlite"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZDIRECTORY TEXT, ZFILENAME TEXT, "
        "ZTRASHEDSTATE INTEGER, ZHIDDEN INTEGER, ZVISIBILITYSTATE INTEGER)"
    )
    con.executemany(
        "INSERT INTO ZASSET (ZDIRECTORY, ZFILENAME, ZTRASHEDSTATE, ZHIDDEN, ZVISIBILITYSTATE) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()
    return path


def test_counts_only_visible_rows(tmp_path: Path) -> None:
    database = make_db(
        tmp_path,
        [
            ("DCIM/100APPLE", "IMG_0001.HEIC", 0, 0, 0),
            ("DCIM/100APPLE", "IMG_0002.HEIC", 1, 0, 0),  # Recently Deleted
            ("DCIM/100APPLE", "IMG_0003.HEIC", 0, 1, 0),  # Hidden
            ("DCIM/100APPLE", "IMG_0004.HEIC", 0, 0, 2),  # not in the main grid
        ],
    )
    snapshot = read_snapshot(database)
    assert snapshot.total == 4
    assert snapshot.visible == 1
    assert snapshot.trashed == 1
    assert snapshot.hidden == 1


def test_keys_ignore_the_extension_so_live_photos_collapse(tmp_path: Path) -> None:
    database = make_db(tmp_path, [("DCIM/100APPLE", "IMG_0001.HEIC", 0, 0, 0)])
    snapshot = read_snapshot(database)
    assert snapshot.visible_keys == {"/DCIM/100APPLE/IMG_0001"}


def test_reconcile_flags_trashed_files_still_on_disk(tmp_path: Path) -> None:
    database = make_db(
        tmp_path,
        [
            ("DCIM/100APPLE", "IMG_0001.HEIC", 0, 0, 0),
            ("DCIM/100APPLE", "IMG_0002.HEIC", 1, 0, 0),
        ],
    )
    files = [
        remote("/DCIM/100APPLE/IMG_0001.HEIC"),
        remote("/DCIM/100APPLE/IMG_0001.MOV"),  # Live Photo half
        remote("/DCIM/100APPLE/IMG_0002.HEIC"),  # deleted in Photos, file still there
    ]
    check = reconcile(read_snapshot(database), files)
    assert check.trashed_on_disk == ["/DCIM/100APPLE/IMG_0002"]
    assert check.missing_files == []
    assert check.untracked == []
    assert check.is_complete


def test_reconcile_reports_library_items_without_a_file(tmp_path: Path) -> None:
    database = make_db(
        tmp_path,
        [
            ("DCIM/100APPLE", "IMG_0001.HEIC", 0, 0, 0),
            ("PhotoData/CPLAssets/group1", "UUID.JPG", 0, 0, 0),
        ],
    )
    check = reconcile(read_snapshot(database), [remote("/DCIM/100APPLE/IMG_0001.HEIC")])
    assert check.missing_files == ["/PhotoData/CPLAssets/group1/UUID"]
    assert not check.is_complete


def test_reconcile_reports_files_with_no_database_row(tmp_path: Path) -> None:
    database = make_db(tmp_path, [("DCIM/100APPLE", "IMG_0001.HEIC", 0, 0, 0)])
    check = reconcile(
        read_snapshot(database),
        [remote("/DCIM/100APPLE/IMG_0001.HEIC"), remote("/DCIM/100APPLE/STRAY.JPG")],
    )
    assert check.untracked == ["/DCIM/100APPLE/STRAY"]


def test_leading_slash_in_directory_is_tolerated(tmp_path: Path) -> None:
    database = make_db(tmp_path, [("/DCIM/100APPLE", "IMG_0001.HEIC", 0, 0, 0)])
    assert read_snapshot(database).visible_keys == {"/DCIM/100APPLE/IMG_0001"}


def test_rows_without_a_path_are_ignored(tmp_path: Path) -> None:
    database = make_db(
        tmp_path,
        [("DCIM/100APPLE", "IMG_0001.HEIC", 0, 0, 0), (None, None, 0, 0, 0)],  # type: ignore[list-item]
    )
    assert read_snapshot(database).total == 1


def test_missing_database_is_a_friendly_error(tmp_path: Path) -> None:
    with pytest.raises(IpmError, match="missing"):
        read_snapshot(tmp_path / "nope.sqlite")


def test_unexpected_schema_is_a_friendly_error(tmp_path: Path) -> None:
    path = tmp_path / "Photos.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE SOMETHINGELSE (x INTEGER)")
    con.commit()
    con.close()
    with pytest.raises(IpmError, match="unexpected layout"):
        read_snapshot(path)


def test_partial_schema_without_hidden_columns_still_works(tmp_path: Path) -> None:
    path = tmp_path / "Photos.sqlite"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE ZASSET (Z_PK INTEGER PRIMARY KEY, ZDIRECTORY TEXT, "
        "ZFILENAME TEXT, ZTRASHEDSTATE INTEGER)"
    )
    con.execute("INSERT INTO ZASSET (ZDIRECTORY, ZFILENAME, ZTRASHEDSTATE) VALUES (?, ?, ?)",
                ("DCIM/100APPLE", "IMG_0001.HEIC", 0))
    con.commit()
    con.close()
    snapshot = read_snapshot(path)
    assert snapshot.visible == 1
    assert snapshot.hidden == 0
