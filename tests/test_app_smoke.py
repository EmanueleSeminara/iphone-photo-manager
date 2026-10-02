"""Smoke tests for the Textual layer: it builds, it renders, buttons start disabled."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from textual.widgets import Button, Checkbox, Input, Static

from ipm.config import Config
from ipm.models import DeleteCandidates, DeleteRequest, DeviceInfo, ImportRecord, RemoteFile
from ipm.tui.app import IpmApp
from ipm.tui.screens import CONFIRM_WORD, ConfirmDeleteScreen
from tests.fakes import make_file
from tests.tui_harness import (  # noqa: F401 - the three fixtures are autouse
    attach,
    config_for,
    no_finder,
    no_real_devices,
    no_real_photo_library,
    record_for,
)


async def test_app_starts_with_empty_placeholders(destination: Path) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test() as pilot:
        await pilot.pause()
        body = app.query_one("#device-body", Static)
        assert "Waiting for an iPhone" in str(body.render())
        assert app.query_one("#import-button", Button).disabled is True
        assert app.query_one("#delete-button", Button).disabled is True
        assert app.query_one("#folder-button", Button).disabled is False


async def test_rescan_is_bound_to_r() -> None:
    keys = {binding[0] for binding in IpmApp.BINDINGS}
    assert "r" in keys
    assert "s" not in keys


async def test_scan_note_stays_visible_during_a_rescan(destination: Path) -> None:
    """A rescan keeps the old counts on screen, so it must say it is working."""
    app = IpmApp(config_for(destination))
    async with app.run_test() as pilot:
        await pilot.pause()
        app._media = [
            RemoteFile(
                path="/DCIM/100APPLE/IMG_0001.HEIC",
                size=10,
                created=datetime(2024, 3, 14),
                modified=datetime(2024, 3, 14),
            )
        ]
        app._device = DeviceInfo(serial="X", name="Fake iPhone")
        app._backend = object()  # type: ignore[assignment]
        app._scan_note = "Scanning… 500 files"
        app._render_device()
        await pilot.pause()

        text = str(app.query_one("#device-body", Static).render())
        assert "Scanning… 500 files" in text
        assert "1 items" in text  # the previous numbers are still shown
        app._backend = None


async def test_manifest_is_created_for_the_destination(destination: Path) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert (destination / ".ipm" / "manifest.db").exists()


async def test_destination_dialog_opens_when_no_folder_is_set(tmp_path: Path) -> None:
    app = IpmApp(Config(destination=None))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.screen.__class__.__name__ == "DestinationScreen"


async def test_confirm_screen_requires_the_typed_word(destination: Path) -> None:
    record = ImportRecord(
        device_udid="UDID",
        device_path="/DCIM/A.HEIC",
        size=3,
        mtime=1.0,
        local_path="2024/03/A.HEIC",
        imported_at=datetime.now(UTC),
    )
    candidates = DeleteCandidates(deletable=[record], unverified=[])

    app = IpmApp(config_for(destination))
    async with app.run_test(size=(100, 45)) as pilot:
        await app.push_screen(ConfirmDeleteScreen(candidates))
        await pilot.pause()
        confirm = app.screen.query_one("#confirm-ok", Button)
        assert confirm.disabled is True

        await pilot.press(*"nope")
        await pilot.pause()
        assert confirm.disabled is True

        for _ in range(4):
            await pilot.press("backspace")
        await pilot.press(*CONFIRM_WORD)
        await pilot.pause()
        assert confirm.disabled is False


async def test_import_asks_before_copying_anything(destination: Path) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test() as pilot:
        await pilot.pause()
        backend = await attach(app)

        app.action_start_import()
        await pilot.pause()
        await pilot.pause()
        assert app.screen.__class__.__name__ == "ConfirmImportScreen"
        assert backend.downloads == []

        await pilot.press("escape")
        await pilot.pause()
        await pilot.pause()
        assert backend.downloads == []
        assert list(destination.glob("2024/*/*")) == []
        app._backend = None


async def test_import_copies_only_after_the_dialog_is_confirmed(destination: Path) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test() as pilot:
        await pilot.pause()
        backend = await attach(app)

        app.action_start_import()
        await pilot.pause()
        await pilot.pause()
        await pilot.click("#import-ok")
        for _ in range(12):
            await pilot.pause()

        assert sorted(backend.downloads) == ["/DCIM/A.HEIC", "/DCIM/B.HEIC"]
        assert (destination / "2024/03/A.HEIC").read_bytes() == b"aaa"
        assert backend.deletions == []
        app._backend = None


async def test_confirm_screen_can_delete_only_the_newest_items(destination: Path) -> None:
    candidates = DeleteCandidates(
        deletable=[
            record_for("/DCIM/A.HEIC", "2024/03/A.HEIC", 3, 100.0),
            record_for("/DCIM/B.HEIC", "2024/03/B.HEIC", 3, 200.0),
            record_for("/DCIM/B.MOV", "2024/03/B.MOV", 4, 201.0),
        ],
        unverified=[],
    )
    chosen: list[DeleteRequest | None] = []

    app = IpmApp(config_for(destination))
    async with app.run_test(size=(100, 45)) as pilot:
        app.push_screen(ConfirmDeleteScreen(candidates), chosen.append)
        await pilot.pause()

        # The default scope is everything.
        await pilot.click("#confirm-input")
        await pilot.press(*CONFIRM_WORD)
        await pilot.pause()
        assert app.screen.query_one("#confirm-ok", Button).disabled is False

        # Switch to "only the most recent", one item: B.HEIC and its Live Photo half.
        await pilot.click("#scope-recent")
        await pilot.pause()
        count = app.screen.query_one("#confirm-count", Input)
        assert count.disabled is False
        count.value = "1"
        await pilot.pause()

        await pilot.click("#confirm-ok")
        await pilot.pause()

    assert chosen and chosen[0] is not None
    assert [record.device_path for record in chosen[0].records] == ["/DCIM/B.HEIC", "/DCIM/B.MOV"]
    assert chosen[0].deep is True  # the thorough check is the default


async def test_confirm_screen_selects_nothing_for_a_zero_count(destination: Path) -> None:
    candidates = DeleteCandidates(
        deletable=[record_for("/DCIM/A.HEIC", "2024/03/A.HEIC", 3, 100.0)], unverified=[]
    )
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(100, 45)) as pilot:
        await app.push_screen(ConfirmDeleteScreen(candidates))
        await pilot.pause()
        await pilot.click("#confirm-input")
        await pilot.press(*CONFIRM_WORD)
        await pilot.click("#scope-recent")
        app.screen.query_one("#confirm-count", Input).value = "0"
        await pilot.pause()

        assert app.screen.query_one("#confirm-ok", Button).disabled is True


async def test_delete_asks_first_and_can_remove_a_single_item(destination: Path) -> None:
    """End to end through the app: nothing leaves the phone before the dialog is answered."""
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(100, 45)) as pilot:
        await pilot.pause()
        backend = await attach(app)

        # Import both files so the manifest has two verified rows.
        app.action_start_import()
        await pilot.pause()
        await pilot.pause()
        await pilot.click("#import-ok")
        for _ in range(12):
            await pilot.pause()
        assert backend.deletions == []

        app.action_start_delete()
        for _ in range(6):
            await pilot.pause()
        assert app.screen.__class__.__name__ == "ConfirmDeleteScreen"
        assert backend.deletions == []

        await pilot.click("#scope-recent")
        app.screen.query_one("#confirm-count", Input).value = "1"
        await pilot.click("#confirm-input")
        await pilot.press(*CONFIRM_WORD)
        await pilot.pause()
        await pilot.click("#confirm-ok")
        for _ in range(12):
            await pilot.pause()

        # B.HEIC is the newer of the two; A.HEIC stays on the phone.
        assert backend.deletions == ["/DCIM/B.HEIC"]
        assert sorted(backend.files) == ["/DCIM/A.HEIC"]
        assert (destination / "2024/03/B.HEIC").read_bytes() == b"bbbb"
        app._backend = None


async def test_unplugging_during_the_dialog_stops_the_deletion(destination: Path) -> None:
    """Confirming a dialog that outlived the cable must not start deleting."""
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(100, 45)) as pilot:
        await pilot.pause()
        backend = await attach(app)

        app.action_start_import()
        await pilot.pause()
        await pilot.pause()
        await pilot.click("#import-ok")
        for _ in range(12):
            await pilot.pause()

        app.action_start_delete()
        for _ in range(6):
            await pilot.pause()
        assert app.screen.__class__.__name__ == "ConfirmDeleteScreen"

        await app._handle_disconnect(backend.serial)
        await pilot.click("#confirm-input")
        await pilot.press(*CONFIRM_WORD)
        await pilot.click("#confirm-ok")
        for _ in range(8):
            await pilot.pause()

        assert backend.deletions == []
        assert sorted(backend.files) == ["/DCIM/A.HEIC", "/DCIM/B.HEIC"]


async def test_a_corrupted_local_copy_stops_the_deletion_of_its_original(
    destination: Path,
) -> None:
    """The deep check is on by default, and it is what saves the photo here."""
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(100, 45)) as pilot:
        await pilot.pause()
        backend = await attach(app)

        app.action_start_import()
        await pilot.pause()
        await pilot.pause()
        await pilot.click("#import-ok")
        for _ in range(12):
            await pilot.pause()

        # Something overwrites a local copy with different bytes of the same length.
        (destination / "2024/03/B.HEIC").write_bytes(b"XXXX")

        app.action_start_delete()
        for _ in range(6):
            await pilot.pause()
        assert app.screen.query_one("#confirm-deep", Checkbox).value is True
        await pilot.click("#confirm-input")
        await pilot.press(*CONFIRM_WORD)
        await pilot.click("#confirm-ok")
        for _ in range(20):
            await pilot.pause()

        assert backend.deletions == ["/DCIM/A.HEIC"]
        assert "/DCIM/B.HEIC" in backend.files


async def test_a_restored_iphone_is_noticed_and_the_history_can_be_tidied(
    destination: Path,
) -> None:
    """The scenario nobody wants to meet unprepared: same UDID, renumbered library."""
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(100, 45)) as pilot:
        await pilot.pause()
        backend = await attach(app)

        app.action_start_import()
        await pilot.pause()
        await pilot.pause()
        await pilot.click("#import-ok")
        for _ in range(12):
            await pilot.pause()

        # The phone is erased and set up again: same paths, brand new photos.
        backend.files["/DCIM/A.HEIC"] = make_file(
            "/DCIM/A.HEIC", b"new", datetime(2026, 1, 5, 12, 0)
        )
        backend.files["/DCIM/B.HEIC"] = make_file(
            "/DCIM/B.HEIC", b"newr", datetime(2026, 1, 6, 12, 0)
        )

        app.action_rescan()
        for _ in range(10):
            await pilot.pause()
        assert app.screen.__class__.__name__ == "LibraryChangedScreen"

        await pilot.click("#changed-tidy")
        for _ in range(8):
            await pilot.pause()

        assert app._database is not None
        assert app._database.live_records(backend.serial) == []
        # Not one file was touched, on either side.
        assert sorted(backend.files) == ["/DCIM/A.HEIC", "/DCIM/B.HEIC"]
        assert (destination / "2024/03/A.HEIC").read_bytes() == b"aaa"
        app._backend = None
