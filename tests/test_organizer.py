"""Folder layout and collision-safe naming."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from ipm.core.organizer import (
    choose_destination,
    is_safe_local_path,
    month_directory,
    relative_local_path,
    sanitise_name,
)
from ipm.models import RemoteFile


def remote(path: str, size: int = 3, created: datetime | None = None) -> RemoteFile:
    stamp = created or datetime(2024, 3, 14, 10, 30)
    return RemoteFile(path=path, size=size, created=stamp, modified=stamp)


def test_month_directory_uses_creation_date(destination: Path) -> None:
    assert month_directory(destination, datetime(2021, 1, 5)) == destination / "2021" / "01"
    assert month_directory(destination, datetime(1999, 12, 31)) == destination / "1999" / "12"


def test_destination_follows_year_month(destination: Path) -> None:
    planned = choose_destination(destination, remote("/DCIM/100APPLE/IMG_0001.HEIC"))
    assert planned.destination == destination / "2024" / "03" / "IMG_0001.HEIC"
    assert planned.already_on_disk is False


def test_extension_does_not_affect_the_folder(destination: Path) -> None:
    stamp = datetime(2022, 7, 9)
    for name in ("IMG_1.HEIC", "IMG_1.MOV", "IMG_1.AAE", "IMG_1.DNG"):
        planned = choose_destination(destination, remote(f"/DCIM/100APPLE/{name}", created=stamp))
        assert planned.destination.parent == destination / "2022" / "07"


def test_live_photo_pair_lands_together(destination: Path) -> None:
    stamp = datetime(2023, 5, 1)
    reserved: set[Path] = set()
    photo = choose_destination(destination, remote("/DCIM/100APPLE/IMG_9.HEIC", created=stamp), reserved)
    movie = choose_destination(destination, remote("/DCIM/100APPLE/IMG_9.MOV", created=stamp), reserved)
    assert photo.destination.parent == movie.destination.parent
    assert photo.destination.name == "IMG_9.HEIC"
    assert movie.destination.name == "IMG_9.MOV"


def test_same_name_different_size_gets_a_suffix(destination: Path) -> None:
    target = destination / "2024" / "03" / "IMG_0001.HEIC"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"different content")

    planned = choose_destination(destination, remote("/DCIM/101APPLE/IMG_0001.HEIC", size=3))
    assert planned.destination.name == "IMG_0001_1.HEIC"
    assert planned.already_on_disk is False
    assert target.read_bytes() == b"different content"


def test_same_name_same_size_and_time_is_adopted(destination: Path) -> None:
    incoming = remote("/DCIM/100APPLE/IMG_0001.HEIC", size=3)
    target = destination / "2024" / "03" / "IMG_0001.HEIC"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"abc")
    stamp = incoming.modified.timestamp()
    os.utime(target, (stamp, stamp))

    planned = choose_destination(destination, incoming)
    assert planned.destination == target
    assert planned.already_on_disk is True


def test_same_name_and_size_but_another_date_is_not_adopted(destination: Path) -> None:
    """A stranger's photo of the same length must not vouch for the device's one."""
    incoming = remote("/DCIM/100APPLE/IMG_0001.HEIC", size=3)
    target = destination / "2024" / "03" / "IMG_0001.HEIC"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"xyz")
    stamp = incoming.modified.timestamp() - 86_400
    os.utime(target, (stamp, stamp))

    planned = choose_destination(destination, incoming)
    assert planned.destination.name == "IMG_0001_1.HEIC"
    assert planned.already_on_disk is False
    assert target.read_bytes() == b"xyz"


def test_suffixes_increment_until_free(destination: Path) -> None:
    folder = destination / "2024" / "03"
    folder.mkdir(parents=True)
    (folder / "IMG_0001.HEIC").write_bytes(b"xxxxxxx")
    (folder / "IMG_0001_1.HEIC").write_bytes(b"yyyyyyy")

    planned = choose_destination(destination, remote("/DCIM/102APPLE/IMG_0001.HEIC", size=3))
    assert planned.destination.name == "IMG_0001_2.HEIC"


def test_reserved_paths_are_not_reused(destination: Path) -> None:
    reserved: set[Path] = set()
    first = choose_destination(destination, remote("/DCIM/100APPLE/IMG_1.HEIC"), reserved)
    second = choose_destination(destination, remote("/DCIM/101APPLE/IMG_1.HEIC"), reserved)
    assert first.destination != second.destination
    assert second.destination.name == "IMG_1_1.HEIC"
    assert reserved == {first.destination, second.destination}


def test_sanitise_name() -> None:
    assert sanitise_name("IMG_0001.HEIC") == "IMG_0001.HEIC"
    assert sanitise_name("we/ird.HEIC") == "we_ird.HEIC"
    assert sanitise_name("") == "unnamed"
    assert sanitise_name(".") == "unnamed"
    assert sanitise_name("bad\0name") == "bad_name"


def test_relative_local_path_is_posix(destination: Path) -> None:
    target = destination / "2024" / "03" / "IMG.HEIC"
    assert relative_local_path(destination, target) == "2024/03/IMG.HEIC"


def test_paths_escaping_the_destination_are_rejected() -> None:
    """The manifest's ``local_path`` is only ever trusted after this check."""
    assert is_safe_local_path("2024/03/IMG_0001.HEIC") is True
    assert is_safe_local_path("IMG_0001.HEIC") is True

    assert is_safe_local_path("") is False
    assert is_safe_local_path("/etc/passwd") is False
    assert is_safe_local_path("../../../etc/passwd") is False
    assert is_safe_local_path("2024/../../outside.HEIC") is False
    assert is_safe_local_path("2024/03/bad\0name") is False
    assert is_safe_local_path("2024\\03\\IMG.HEIC") is False


def test_chosen_destinations_are_always_safe() -> None:
    """Whatever the device names a file, the recorded path stays inside the root."""
    root = Path("/library")
    for name in ("../../evil.HEIC", "/absolute.HEIC", "..", ".", "a:b.HEIC", "x\0y.HEIC"):
        planned = choose_destination(root, remote(f"/DCIM/{name}"))
        assert is_safe_local_path(relative_local_path(root, planned.destination))
