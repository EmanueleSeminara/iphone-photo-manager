"""Import behaviour: byte fidelity, resume, atomicity, parallelism, errors."""

from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime
from pathlib import Path

from ipm.core.database import ManifestDatabase
from ipm.core.digest import digest_of
from ipm.core.importer import PART_SUFFIX, Importer, ImportProgress, cleanup_partials
from tests.fakes import FakeBackend, make_file

MARCH = datetime(2024, 3, 14, 9, 0, 0)
APRIL = datetime(2024, 4, 2, 18, 30, 0)


def build_importer(
    backend: FakeBackend,
    database: ManifestDatabase,
    destination: Path,
    **kwargs: object,
) -> Importer:
    return Importer(backend, database, destination, **kwargs)  # type: ignore[arg-type]


async def test_copies_every_file_into_year_month_folders(
    database: ManifestDatabase, destination: Path
) -> None:
    backend = FakeBackend.with_files(
        make_file("/DCIM/100APPLE/IMG_0001.HEIC", b"photo-bytes", MARCH),
        make_file("/DCIM/100APPLE/IMG_0001.MOV", b"live-photo-movie", MARCH),
        make_file("/DCIM/100APPLE/IMG_0002.DNG", b"raw", APRIL),
        make_file("/DCIM/100APPLE/IMG_0002.AAE", b"<sidecar/>", APRIL),
    )
    stats = await build_importer(backend, database, destination).run()

    assert stats.copied == 4
    assert stats.failed == 0
    assert (destination / "2024/03/IMG_0001.HEIC").read_bytes() == b"photo-bytes"
    assert (destination / "2024/03/IMG_0001.MOV").read_bytes() == b"live-photo-movie"
    assert (destination / "2024/04/IMG_0002.DNG").read_bytes() == b"raw"
    assert (destination / "2024/04/IMG_0002.AAE").read_bytes() == b"<sidecar/>"
    assert stats.bytes_copied == sum(len(item.content) for item in backend.files.values())


async def test_copy_is_byte_identical_and_keeps_mtime(
    database: ManifestDatabase, destination: Path
) -> None:
    payload = bytes(range(256)) * 8
    modified = datetime(2024, 3, 14, 9, 0, 0)
    backend = FakeBackend.with_files(
        make_file("/DCIM/100APPLE/IMG_0001.HEIC", payload, MARCH, modified)
    )
    await build_importer(backend, database, destination).run()

    local = destination / "2024/03/IMG_0001.HEIC"
    assert local.read_bytes() == payload
    assert int(local.stat().st_mtime) == int(modified.timestamp())


async def test_second_run_skips_everything(database: ManifestDatabase, destination: Path) -> None:
    backend = FakeBackend.with_files(make_file("/DCIM/A.HEIC", b"abc", MARCH))
    await build_importer(backend, database, destination).run()
    backend.downloads.clear()

    stats = await build_importer(backend, database, destination).run()
    assert stats.copied == 0
    assert stats.skipped == 1
    assert backend.downloads == []


async def test_missing_local_copy_is_reimported(
    database: ManifestDatabase, destination: Path
) -> None:
    backend = FakeBackend.with_files(make_file("/DCIM/A.HEIC", b"abc", MARCH))
    await build_importer(backend, database, destination).run()
    (destination / "2024/03/A.HEIC").unlink()

    stats = await build_importer(backend, database, destination).run()
    assert stats.copied == 1
    assert (destination / "2024/03/A.HEIC").read_bytes() == b"abc"
    # The name is reused rather than suffixed.
    assert not (destination / "2024/03/A_1.HEIC").exists()


async def test_files_already_deleted_from_device_are_not_recopied(
    database: ManifestDatabase, destination: Path
) -> None:
    backend = FakeBackend.with_files(make_file("/DCIM/A.HEIC", b"abc", MARCH))
    await build_importer(backend, database, destination).run()
    database.mark_deleted(backend.serial, ["/DCIM/A.HEIC"])
    (destination / "2024/03/A.HEIC").unlink()
    backend.downloads.clear()

    stats = await build_importer(backend, database, destination).run()
    assert stats.copied == 0
    assert stats.skipped == 1
    assert backend.downloads == []


async def test_new_files_only_are_copied_on_a_later_run(
    database: ManifestDatabase, destination: Path
) -> None:
    backend = FakeBackend.with_files(make_file("/DCIM/A.HEIC", b"abc", MARCH))
    await build_importer(backend, database, destination).run()

    backend.files["/DCIM/B.HEIC"] = make_file("/DCIM/B.HEIC", b"defg", APRIL)
    backend.downloads.clear()
    stats = await build_importer(backend, database, destination).run()

    assert stats.copied == 1
    assert stats.skipped == 1
    assert backend.downloads == ["/DCIM/B.HEIC"]


async def test_partial_file_is_removed_and_nothing_is_written_on_failure(
    database: ManifestDatabase, destination: Path
) -> None:
    backend = FakeBackend.with_files(
        make_file("/DCIM/GOOD.HEIC", b"abc", MARCH),
        make_file("/DCIM/BAD.HEIC", b"abcdef", MARCH),
        fail_paths={"/DCIM/BAD.HEIC"},
    )
    stats = await build_importer(backend, database, destination).run()

    assert stats.copied == 1
    assert stats.failed == 1
    assert stats.errors and "BAD.HEIC" in stats.errors[0]
    assert (destination / "2024/03/GOOD.HEIC").exists()
    assert not (destination / "2024/03/BAD.HEIC").exists()
    assert list(destination.rglob(f"*{PART_SUFFIX}")) == []
    # The failed file is not recorded, so the next run retries it.
    assert database.get(backend.serial, "/DCIM/BAD.HEIC") is None


async def test_short_transfer_is_treated_as_a_failure(
    database: ManifestDatabase, destination: Path
) -> None:
    backend = FakeBackend.with_files(make_file("/DCIM/A.HEIC", b"abcdef", MARCH))

    original = backend.download_file

    async def truncated(remote_path: str, dst: Path, on_chunk=None) -> int:  # type: ignore[no-untyped-def]
        await original(remote_path, dst, on_chunk)
        dst.write_bytes(b"abc")
        return 3

    backend.download_file = truncated  # type: ignore[method-assign]
    stats = await build_importer(backend, database, destination).run()

    assert stats.copied == 0
    assert stats.failed == 1
    assert not (destination / "2024/03/A.HEIC").exists()
    assert list(destination.rglob(f"*{PART_SUFFIX}")) == []


async def test_cancellation_stops_the_run_and_leaves_no_partials(
    database: ManifestDatabase, destination: Path
) -> None:
    files = [make_file(f"/DCIM/IMG_{index:03d}.HEIC", b"x" * 64, MARCH) for index in range(30)]
    backend = FakeBackend.with_files(*files, chunk_size=8)
    cancel = asyncio.Event()

    # Cancel from inside the third transfer, so at least one file is aborted mid-way.
    original = backend.download_file

    async def cancelling_download(remote_path: str, dst: Path, on_chunk=None) -> int:  # type: ignore[no-untyped-def]
        if len(backend.downloads) >= 2:
            cancel.set()
        return await original(remote_path, dst, on_chunk)

    backend.download_file = cancelling_download  # type: ignore[method-assign]

    stats = await build_importer(
        backend, database, destination, concurrency=1, cancel=cancel
    ).run()

    assert stats.cancelled is True
    assert stats.copied < len(files)
    assert list(destination.rglob(f"*{PART_SUFFIX}")) == []
    # Everything that was copied is recorded, so a later run resumes cleanly.
    assert len(database.records_for_device(backend.serial)) == stats.copied

    resumed = await build_importer(backend, database, destination).run()
    assert resumed.copied + resumed.skipped == len(files)
    for item in files:
        assert (destination / "2024/03" / item.path.rsplit("/", 1)[-1]).read_bytes() == item.content


async def test_concurrency_transfers_every_file_exactly_once(
    database: ManifestDatabase, destination: Path
) -> None:
    files = [make_file(f"/DCIM/IMG_{index:03d}.HEIC", b"y" * 32, MARCH) for index in range(20)]
    backend = FakeBackend.with_files(*files)

    stats = await build_importer(backend, database, destination, concurrency=4).run()

    assert stats.copied == 20
    assert sorted(backend.downloads) == sorted(item.path for item in files)
    assert len(database.records_for_device(backend.serial)) == 20


async def test_untracked_identical_copy_is_adopted_without_transfer(
    database: ManifestDatabase, destination: Path
) -> None:
    """A copy this app wrote before (same size, same restored mtime) is reused."""
    target = destination / "2024/03/A.HEIC"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"abc")
    os.utime(target, (MARCH.timestamp(), MARCH.timestamp()))
    backend = FakeBackend.with_files(make_file("/DCIM/A.HEIC", b"abc", MARCH))

    stats = await build_importer(backend, database, destination).run()

    assert backend.downloads == []
    assert stats.skipped == 1
    record = database.get(backend.serial, "/DCIM/A.HEIC")
    assert record is not None and record.local_path == "2024/03/A.HEIC"


async def test_untracked_different_copy_is_kept_and_new_name_used(
    database: ManifestDatabase, destination: Path
) -> None:
    target = destination / "2024/03/A.HEIC"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"someone else's photo")
    backend = FakeBackend.with_files(make_file("/DCIM/A.HEIC", b"abc", MARCH))

    await build_importer(backend, database, destination).run()

    assert target.read_bytes() == b"someone else's photo"
    assert (destination / "2024/03/A_1.HEIC").read_bytes() == b"abc"


async def test_progress_reports_bytes_and_files(
    database: ManifestDatabase, destination: Path
) -> None:
    backend = FakeBackend.with_files(
        make_file("/DCIM/A.HEIC", b"a" * 100, MARCH),
        make_file("/DCIM/B.HEIC", b"b" * 100, MARCH),
        chunk_size=10,
    )
    seen: list[ImportProgress] = []
    await build_importer(
        backend, database, destination, concurrency=1, on_progress=seen.append
    ).run()

    assert seen, "expected progress callbacks"
    last = seen[-1]
    assert last.phase == "done"
    assert last.bytes_total == 200
    assert last.bytes_done == 200
    assert last.files_done == 2
    assert last.fraction == 1.0


def test_cleanup_partials_removes_leftovers(destination: Path) -> None:
    folder = destination / "2024" / "03"
    folder.mkdir(parents=True)
    (folder / f"IMG.HEIC{PART_SUFFIX}").write_bytes(b"half")
    (folder / "IMG.HEIC").write_bytes(b"whole")

    assert cleanup_partials(destination) == 1
    assert not (folder / f"IMG.HEIC{PART_SUFFIX}").exists()
    assert (folder / "IMG.HEIC").exists()


async def test_files_the_iphone_never_had_are_left_alone(
    database: ManifestDatabase, destination: Path
) -> None:
    """The destination may be an existing photo archive; the import only adds to it."""
    archive = destination / "2019" / "07"
    archive.mkdir(parents=True)
    (archive / "scan_holidays.jpg").write_bytes(b"an old scan")
    (destination / "notes.txt").write_bytes(b"my own file")
    backend = FakeBackend.with_files(make_file("/DCIM/A.HEIC", b"abc", MARCH))

    await build_importer(backend, database, destination).run()
    cleanup_partials(destination)

    assert (archive / "scan_holidays.jpg").read_bytes() == b"an old scan"
    assert (destination / "notes.txt").read_bytes() == b"my own file"
    # They are not in the manifest either, so the delete button never sees them.
    assert [record.local_path for record in database.records_for_device(backend.serial)] == [
        "2024/03/A.HEIC"
    ]


async def test_the_digest_recorded_is_the_digest_of_the_bytes_received(
    database: ManifestDatabase, destination: Path
) -> None:
    content = b"photo bytes" * 100
    backend = FakeBackend.with_files(make_file("/DCIM/A.HEIC", content, MARCH))

    await build_importer(backend, database, destination).run()

    record = database.get(backend.serial, "/DCIM/A.HEIC")
    assert record is not None
    assert record.sha256 == hashlib.sha256(content).hexdigest()
    # And it describes what is on disk, since the bytes are copied untouched.
    assert record.sha256 == digest_of(destination / "2024/03/A.HEIC")


async def test_an_adopted_copy_is_hashed_from_disk(
    database: ManifestDatabase, destination: Path
) -> None:
    target = destination / "2024/03/A.HEIC"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"abc")
    os.utime(target, (MARCH.timestamp(), MARCH.timestamp()))
    backend = FakeBackend.with_files(make_file("/DCIM/A.HEIC", b"abc", MARCH))

    await build_importer(backend, database, destination).run()

    record = database.get(backend.serial, "/DCIM/A.HEIC")
    assert backend.downloads == []
    assert record is not None and record.sha256 == hashlib.sha256(b"abc").hexdigest()


async def test_a_failed_transfer_records_no_digest(
    database: ManifestDatabase, destination: Path
) -> None:
    backend = FakeBackend.with_files(
        make_file("/DCIM/A.HEIC", b"abc", MARCH), fail_paths={"/DCIM/A.HEIC"}
    )

    stats = await build_importer(backend, database, destination).run()

    assert stats.failed == 1
    assert database.get(backend.serial, "/DCIM/A.HEIC") is None


async def test_a_lost_manifest_is_rebuilt_without_downloading_anything(
    database: ManifestDatabase, destination: Path, tmp_path: Path
) -> None:
    """The `--keep-photos` reset, end to end: photos kept, history thrown away.

    The next import must recognise every file it already has, hash it, write the
    rows again -- and transfer nothing. Anything else would mean 32 GB over USB 2
    for no reason, or a folder full of `_1` duplicates.
    """
    backend = FakeBackend.with_files(
        make_file("/DCIM/A.HEIC", b"aaa", MARCH),
        make_file("/DCIM/B.HEIC", b"bbbb", APRIL),
    )
    await build_importer(backend, database, destination).run()
    digests = {
        record.device_path: record.sha256
        for record in database.records_for_device(backend.serial)
    }
    backend.downloads.clear()

    # scripts/reset-test-state.sh --keep-photos: the manifest goes, the bytes stay.
    database._conn.execute("DELETE FROM files")  # noqa: SLF001
    database._conn.commit()  # noqa: SLF001

    plan = await build_importer(backend, database, destination).build_plan(
        await backend.list_media()
    )
    assert plan.file_count == 2
    assert plan.adopted == 2
    assert plan.to_transfer == 0
    assert plan.total_bytes == 0

    stats = await build_importer(backend, database, destination).run(plan=plan)

    assert backend.downloads == []          # not one byte crossed the cable
    assert stats.copied == 0
    assert stats.skipped == 2
    assert sorted(p.name for p in destination.glob("20*/*/*")) == ["A.HEIC", "B.HEIC"]
    # The history is back, digests and all, so deletion is possible again.
    rebuilt = {
        record.device_path: record.sha256
        for record in database.records_for_device(backend.serial)
    }
    assert rebuilt == digests


async def test_a_touched_local_file_is_not_adopted_but_kept(
    database: ManifestDatabase, destination: Path
) -> None:
    """If something rewrote the timestamps, the file is no longer recognisable."""
    backend = FakeBackend.with_files(make_file("/DCIM/A.HEIC", b"aaa", MARCH))
    await build_importer(backend, database, destination).run()
    backend.downloads.clear()
    database._conn.execute("DELETE FROM files")  # noqa: SLF001
    database._conn.commit()  # noqa: SLF001
    os.utime(destination / "2024/03/A.HEIC", (0, 0))

    await build_importer(backend, database, destination).run()

    # Both survive: the unrecognised one is left alone, the phone's copy lands beside it.
    assert (destination / "2024/03/A.HEIC").read_bytes() == b"aaa"
    assert (destination / "2024/03/A_1.HEIC").read_bytes() == b"aaa"
    assert backend.downloads == ["/DCIM/A.HEIC"]
