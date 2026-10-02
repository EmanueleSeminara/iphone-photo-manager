"""What the log says after a deletion, for each of the ways a phone can answer.

Measured on an iPhone 13 on 2026-10-02: one run removed five single photos and left
five Live Photos whole -- both halves of each still on the phone, untouched. The report
said those ten files were "still on the iPhone although their photo was deleted", the
sentence meant for a Live Photo half left behind by a deleted still. The two cases now
have a sentence each, and the run where nothing went says what to do next.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ipm.core.database import ManifestDatabase
from ipm.core.deleter import Deleter
from ipm.core.importer import Importer
from ipm.models import DeleteStats
from ipm.tui.app import IpmApp
from tests.fakes import FakeAssetService, FakeBackend, make_file
from tests.tui_harness import (  # noqa: F401 - the three fixtures are autouse
    activity_log,
    config_for,
    no_finder,
    no_real_devices,
    no_real_photo_library,
    settle,
)

MARCH = datetime(2024, 3, 14, 9, 0, 0)


def _phone() -> FakeBackend:
    """A single photo and a Live Photo."""
    return FakeBackend.with_files(
        make_file("/DCIM/IMG_1.PNG", b"single", MARCH),
        make_file("/DCIM/IMG_2.HEIC", b"still", MARCH),
        make_file("/DCIM/IMG_2.MOV", b"motion", MARCH),
    )


async def _run(
    backend: FakeBackend, assets: FakeAssetService, database: ManifestDatabase, destination: Path
) -> DeleteStats:
    await Importer(backend, database, destination).run()
    deleter = Deleter(
        backend, database, destination, assets=assets, device_files=await backend.list_media()
    )
    return await deleter.run()


async def test_an_item_the_phone_ignored_is_reported_as_untouched(
    database: ManifestDatabase, destination: Path
) -> None:
    backend = _phone()
    assets = FakeAssetService(backend, fail_identifiers={"/DCIM/IMG_2.HEIC"})

    stats = await _run(backend, assets, database, destination)

    assert stats.items_deleted == 1
    assert stats.leftover == ["/DCIM/IMG_2.HEIC", "/DCIM/IMG_2.MOV"]
    assert stats.items_untouched == 1
    assert stats.untouched == ["/DCIM/IMG_2.HEIC", "/DCIM/IMG_2.MOV"]


async def test_a_half_left_behind_is_not_called_untouched(
    database: ManifestDatabase, destination: Path
) -> None:
    backend = _phone()
    assets = FakeAssetService(backend, leave_related=True)

    stats = await _run(backend, assets, database, destination)

    assert stats.leftover == ["/DCIM/IMG_2.MOV"]
    assert stats.untouched == []
    assert stats.items_untouched == 0


async def test_a_clean_run_reports_nothing_left(
    database: ManifestDatabase, destination: Path
) -> None:
    backend = _phone()
    stats = await _run(backend, FakeAssetService(backend), database, destination)
    assert stats.leftover == []
    assert stats.untouched == []


def _report(app: IpmApp, stats: DeleteStats) -> str:
    app._report_deletion(stats)
    return " ".join(activity_log(app).split())


async def test_the_log_tells_an_ignored_item_from_a_half_left_behind(destination: Path) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(120, 50)) as pilot:
        await settle(pilot, 3)
        text = _report(
            app,
            DeleteStats(
                deleted=1,
                items_deleted=1,
                failed=3,
                leftover=["/DCIM/IMG_2.HEIC", "/DCIM/IMG_2.MOV", "/DCIM/IMG_3.MOV"],
                untouched=["/DCIM/IMG_2.HEIC", "/DCIM/IMG_2.MOV"],
                items_untouched=1,
            ),
        )
        assert "1 item(s) were not removed — the iPhone ignored that part of the request" in text
        assert "Press d again" in text
        assert "1 file(s) are still on the iPhone although their photo was deleted" in text
        # The ignored Live Photo is listed once, under its own sentence.
        assert text.count("/DCIM/IMG_2.MOV") == 1


async def test_ignored_items_alone_never_read_as_halves(destination: Path) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(120, 50)) as pilot:
        await settle(pilot, 3)
        text = _report(
            app,
            DeleteStats(
                deleted=1,
                items_deleted=1,
                failed=2,
                leftover=["/DCIM/IMG_2.HEIC", "/DCIM/IMG_2.MOV"],
                untouched=["/DCIM/IMG_2.HEIC", "/DCIM/IMG_2.MOV"],
                items_untouched=1,
            ),
        )
        assert "were not removed" in text
        assert "although their photo was deleted" not in text


async def test_a_run_that_removed_nothing_says_to_try_again(destination: Path) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(120, 50)) as pilot:
        await settle(pilot, 3)
        text = _report(
            app,
            DeleteStats(
                failed=2,
                leftover=["/DCIM/IMG_2.HEIC", "/DCIM/IMG_2.MOV"],
                untouched=["/DCIM/IMG_2.HEIC", "/DCIM/IMG_2.MOV"],
                items_untouched=1,
            ),
        )
        assert "The iPhone accepted the request but removed nothing" in text
        assert "press d again" in text
        assert "although their photo was deleted" not in text
