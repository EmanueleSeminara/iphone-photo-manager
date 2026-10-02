"""Which items are "the most recent" is decided by when they were taken.

Measured on an iPhone 13 on 2026-10-02: two PNGs saved in September,
``IMG_7608.PNG`` and ``IMG_7609.PNG``, reported a *modification* time of 21:42 that
day -- seven hours in the future -- while their *creation* time said September. Dated
by modification time they were the newest items on the phone, so "delete the 10 most
recent items" would have taken them ahead of photos shot minutes earlier. Each file is
now dated by the earlier of its two timestamps. The timestamps below are the real
ones, rounded to the second.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from textual.widgets import Input, Static

from ipm.core.database import ManifestDatabase
from ipm.core.deleter import collect_candidates
from ipm.core.importer import Importer
from ipm.core.items import capture_times, group_by_item, select_newest_items
from ipm.tui.app import IpmApp
from ipm.tui.screens import CONFIRM_WORD
from tests.fakes import FakeBackend, make_file
from tests.tui_harness import (  # noqa: F401 - the three fixtures are autouse
    attach,
    config_for,
    no_finder,
    no_real_devices,
    no_real_photo_library,
    settle,
)

SEPTEMBER = datetime(2026, 9, 14, 18, 20, 5)
FUTURE = datetime(2026, 10, 2, 21, 42, 58)
"""What the phone reported as the modification time, hours after the measurement."""
SHOT = datetime(2026, 10, 2, 14, 33, 6)
EARLIER_SHOT = datetime(2026, 10, 2, 14, 31, 2)


def _phone() -> FakeBackend:
    return FakeBackend.with_files(
        make_file("/DCIM/117APPLE/IMG_7609.PNG", b"png", SEPTEMBER, FUTURE),
        make_file("/DCIM/117APPLE/IMG_7650.HEIC", b"older", EARLIER_SHOT),
        make_file("/DCIM/117APPLE/IMG_7661.HEIC", b"still", SHOT),
        make_file("/DCIM/117APPLE/IMG_7661.MOV", b"motion", SHOT),
    )


def test_a_file_is_dated_by_the_earlier_of_its_two_timestamps() -> None:
    times = capture_times(_phone().files[path].remote for path in _phone().files)
    assert times["/DCIM/117APPLE/IMG_7609.PNG"] == SEPTEMBER.timestamp()
    assert times["/DCIM/117APPLE/IMG_7661.HEIC"] == SHOT.timestamp()


async def test_a_future_modification_time_does_not_make_an_old_file_the_newest(
    database: ManifestDatabase, destination: Path
) -> None:
    backend = _phone()
    await Importer(backend, database, destination).run()
    candidates = collect_candidates(
        database, destination, backend.serial, await backend.list_media()
    )

    groups = group_by_item(candidates.deletable, candidates.taken)
    assert [group.key for group in groups] == [
        "/DCIM/117APPLE/IMG_7661",
        "/DCIM/117APPLE/IMG_7650",
        "/DCIM/117APPLE/IMG_7609",
    ]
    # One item asked for: the photo taken last, with its Live Photo half.
    assert [
        record.device_path
        for record in select_newest_items(candidates.deletable, 1, candidates.taken)
    ] == ["/DCIM/117APPLE/IMG_7661.HEIC", "/DCIM/117APPLE/IMG_7661.MOV"]


async def test_without_the_device_dates_the_manifest_mtime_still_decides(
    database: ManifestDatabase, destination: Path
) -> None:
    """The fallback the rule replaced, kept honest: it is what the bug looked like."""
    backend = _phone()
    await Importer(backend, database, destination).run()
    candidates = collect_candidates(database, destination, backend.serial)

    assert candidates.taken == {}
    newest = group_by_item(candidates.deletable)[0]
    assert newest.key == "/DCIM/117APPLE/IMG_7609"


async def test_the_dialog_deletes_the_photo_taken_last(destination: Path) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(100, 50)) as pilot:
        await settle(pilot, 3)
        backend = await attach(app, *_phone().files.values())

        app.action_start_import()
        await settle(pilot, 4)
        await pilot.click("#import-ok")
        await settle(pilot)

        app.action_start_delete()
        await settle(pilot, 8)
        assert app.screen.__class__.__name__ == "ConfirmDeleteScreen"
        await pilot.click("#scope-recent")
        app.screen.query_one("#confirm-count", Input).value = "1"
        await settle(pilot, 2)
        summary = str(app.screen.query_one("#confirm-selection", Static).render())
        # The range shown is the date of the photo that will go, not of the PNG.
        assert "02/10/2026" in summary
        assert "14/09/2026" not in summary

        await pilot.click("#confirm-input")
        await pilot.press(*CONFIRM_WORD)
        await pilot.click("#confirm-ok")
        await settle(pilot, 16)

        assert sorted(backend.deletions) == [
            "/DCIM/117APPLE/IMG_7661.HEIC",
            "/DCIM/117APPLE/IMG_7661.MOV",
        ]
        assert "/DCIM/117APPLE/IMG_7609.PNG" in backend.files
