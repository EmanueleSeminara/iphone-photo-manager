"""The parts of ``MANUAL_TESTING.md`` that do not need an iPhone, driven for real.

Every test here is named after the numbered check it replaces, so the checklist and
this file can be read side by side. What they exercise is the whole application --
key presses, modal dialogs, workers, the manifest on disk -- against
:class:`~tests.fakes.FakeBackend` and :class:`~tests.fakes.FakeAssetService` instead
of hardware.

What that buys, and what it does not: these tests prove the app behaves as the
checklist says *given a phone that behaves as we believe iPhones behave*. They
cannot prove the belief. The checks that depend on it -- what iOS does to a Live
Photo when its still is deleted, whether the Photos app is left clean -- stay in
``MANUAL_TESTING.md`` (16b, 17, 18) and stay manual.

Checks covered here: 1, 2, 3, 4, 6, 10, 11, 13 (partly), 14, 16, 19, 20, 21.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import Button, Checkbox, Input, ProgressBar, Static

from ipm.config import Config, config_path, load_config
from ipm.tui.app import IpmApp
from ipm.tui.screens import CONFIRM_WORD
from ipm.tui.theme import THEME_NAME
from tests.fakes import FakeBackend, make_file
from tests.tui_harness import (  # noqa: F401 - the three fixtures are autouse
    activity_log,
    attach,
    config_for,
    manifest_files,
    no_finder,
    no_real_devices,
    no_real_photo_library,
    settle,
)

# ---------------------------------------------------------------------------
# 1. Empty state -- no phone connected
# ---------------------------------------------------------------------------


async def test_1_nothing_is_offered_without_a_phone(destination: Path) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test() as pilot:
        await settle(pilot, 3)
        body = str(app.query_one("#device-body", Static).render())
        assert "Waiting for an iPhone" in body
        assert app.query_one("#import-button", Button).disabled is True
        assert app.query_one("#delete-button", Button).disabled is True
        # Changing the folder is the one thing that works with no phone attached.
        assert app.query_one("#folder-button", Button).disabled is False


async def test_1_the_keys_the_footer_promises_all_exist(destination: Path) -> None:
    keys = {binding[0] for binding in IpmApp.BINDINGS}
    assert keys == {"i", "d", "f", "r", "v", "q", "escape"}


async def test_1_stop_is_offered_only_while_there_is_something_to_stop(
    destination: Path,
) -> None:
    """The footer lists what can be done now, so Stop is hidden when idle."""
    app = IpmApp(config_for(destination))
    async with app.run_test() as pilot:
        await settle(pilot, 3)
        assert app.check_action("stop", ()) is None

        app._busy = True
        assert app.check_action("stop", ()) is True


# ---------------------------------------------------------------------------
# 2. First run asks for a folder
# ---------------------------------------------------------------------------


async def test_2_a_typed_folder_is_created_and_remembered(tmp_path: Path) -> None:
    chosen = tmp_path / "brand-new"
    app = IpmApp(Config(destination=None))
    async with app.run_test() as pilot:
        await settle(pilot, 4)
        assert app.screen.__class__.__name__ == "DestinationScreen"

        app.screen.query_one("#destination-input", Input).value = str(chosen)
        await pilot.click("#destination-ok")
        await settle(pilot)

        assert app.config.destination == chosen
        assert chosen.is_dir()
        assert (chosen / ".ipm" / "manifest.db").exists()
        # Remembered across runs: the settings file was written, not just the object.
        assert config_path().exists()
        assert load_config().destination == chosen


async def test_2_a_folder_that_cannot_be_created_is_explained_not_crashed(
    tmp_path: Path,
) -> None:
    """A path under a regular file cannot be a directory -- the dialog must say so."""
    blocker = tmp_path / "a-file"
    blocker.write_text("not a folder")

    app = IpmApp(Config(destination=None))
    async with app.run_test() as pilot:
        await settle(pilot, 4)
        app.screen.query_one("#destination-input", Input).value = str(blocker / "sub")
        await pilot.click("#destination-ok")
        await settle(pilot, 4)

        # Still on the dialog, with a readable reason and no traceback.
        assert app.screen.__class__.__name__ == "DestinationScreen"
        error = str(app.screen.query_one("#destination-error", Static).render())
        assert "Cannot use that folder" in error
        assert app.config.destination is None


# ---------------------------------------------------------------------------
# 3. Live detection
# ---------------------------------------------------------------------------


async def test_3_the_panel_fills_on_connect_and_empties_on_unplug(destination: Path) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test() as pilot:
        await settle(pilot, 3)
        backend = await attach(app)
        app._render_all()
        await settle(pilot, 3)

        body = str(app.query_one("#device-body", Static).render())
        assert "Test iPhone" in body
        assert "2 items" in body
        assert app.query_one("#import-button", Button).disabled is False

        await app._handle_disconnect(backend.serial)
        await settle(pilot, 4)

        assert "Waiting for an iPhone" in str(app.query_one("#device-body", Static).render())
        assert app.query_one("#import-button", Button).disabled is True
        assert app.query_one("#delete-button", Button).disabled is True


# ---------------------------------------------------------------------------
# 4. The numbers are the right numbers
# ---------------------------------------------------------------------------


async def test_4_a_live_photo_counts_as_two_files_but_one_item(destination: Path) -> None:
    """The distinction the whole delete scope rests on, shown in the panel."""
    app = IpmApp(config_for(destination))
    async with app.run_test() as pilot:
        await settle(pilot, 3)
        await attach(
            app,
            make_file("/DCIM/A.HEIC", b"aaa", datetime(2024, 3, 1, 9, 0)),
            make_file("/DCIM/A.MOV", b"aaaaaa", datetime(2024, 3, 1, 9, 0)),
            make_file("/DCIM/A.AAE", b"e", datetime(2024, 3, 1, 9, 0)),
            make_file("/DCIM/B.HEIC", b"bbbb", datetime(2024, 3, 2, 9, 0)),
        )
        app._render_all()
        await settle(pilot, 3)

        body = str(app.query_one("#device-body", Static).render())
        assert "2 items" in body
        assert "Files      4 ·" in body
        assert "+2 parts" in body


# ---------------------------------------------------------------------------
# 6. The import dialog does not import
# ---------------------------------------------------------------------------


async def test_6_escape_on_the_import_dialog_copies_nothing(destination: Path) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test() as pilot:
        await settle(pilot, 3)
        backend = await attach(app)

        app.action_start_import()
        await settle(pilot, 4)
        assert app.screen.__class__.__name__ == "ConfirmImportScreen"

        await pilot.press("escape")
        await settle(pilot)

        assert backend.downloads == []
        assert list(destination.glob("2024/*/*")) == []
        assert "Import cancelled" in activity_log(app)


# ---------------------------------------------------------------------------
# 10. A lost history rebuilds itself, without downloading anything
# ---------------------------------------------------------------------------


async def test_10_a_lost_manifest_is_rebuilt_without_downloading(destination: Path) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test() as pilot:
        await settle(pilot, 3)
        backend = await attach(app)

        app.action_start_import()
        await settle(pilot, 4)
        await pilot.click("#import-ok")
        await settle(pilot)
        assert sorted(backend.downloads) == ["/DCIM/A.HEIC", "/DCIM/B.HEIC"]

        # reset-test-state.sh --keep-photos: the photos stay, the history goes.
        assert app._database is not None
        app._database.close()
        for path in manifest_files(destination):
            path.unlink()
        app._open_database()
        app._refresh_status()
        await settle(pilot)
        assert app._status.verified == 0

        backend.downloads.clear()
        app.action_start_import()
        await settle(pilot, 4)
        summary = " ".join(
            str(node.render()) for node in app.screen.query(Static)
        )
        assert "already in that folder" in summary
        assert "not downloaded again" in summary

        await pilot.click("#import-ok")
        await settle(pilot)

        assert backend.downloads == []  # adopted from disk, not pulled again
        assert app._status.verified == 2


# ---------------------------------------------------------------------------
# 11. Importing again changes nothing
# ---------------------------------------------------------------------------


async def test_11_a_second_import_says_there_is_nothing_to_do(destination: Path) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test() as pilot:
        await settle(pilot, 3)
        backend = await attach(app)

        app.action_start_import()
        await settle(pilot, 4)
        await pilot.click("#import-ok")
        await settle(pilot)

        backend.downloads.clear()
        app.action_start_import()
        await settle(pilot)

        # No dialog at all this time, and nothing transferred.
        assert app.screen.__class__.__name__ != "ConfirmImportScreen"
        assert backend.downloads == []
        assert "Nothing to import" in activity_log(app)
        # No duplicates on disk.
        assert sorted(p.name for p in destination.glob("2024/*/*")) == ["A.HEIC", "B.HEIC"]


# ---------------------------------------------------------------------------
# 13. Interruption and resume (the part that needs no cable)
# ---------------------------------------------------------------------------


async def test_13_a_failed_transfer_leaves_no_partial_and_resumes(destination: Path) -> None:
    """A file that fails mid-transfer must not leave a ``.ipm-part`` behind."""
    app = IpmApp(config_for(destination))
    async with app.run_test() as pilot:
        await settle(pilot, 3)
        backend = await attach(app)
        backend.fail_paths = {"/DCIM/B.HEIC"}

        app.action_start_import()
        await settle(pilot, 4)
        await pilot.click("#import-ok")
        await settle(pilot)

        assert (destination / "2024/03/A.HEIC").exists()
        assert not (destination / "2024/03/B.HEIC").exists()
        assert list(destination.rglob("*.ipm-part")) == []

        # Plug it back in and press Import again: only the missing one is fetched.
        backend.fail_paths.clear()
        backend.downloads.clear()
        app.action_start_import()
        await settle(pilot, 4)
        await pilot.click("#import-ok")
        await settle(pilot)

        assert backend.downloads == ["/DCIM/B.HEIC"]
        assert (destination / "2024/03/B.HEIC").read_bytes() == b"bbbb"
        assert list(destination.rglob("*.ipm-part")) == []


# ---------------------------------------------------------------------------
# 14. A missing local copy is never deletable
# ---------------------------------------------------------------------------


async def test_14_a_missing_local_copy_is_listed_as_kept_and_never_deleted(
    destination: Path,
) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(100, 45)) as pilot:
        await settle(pilot, 3)
        backend = await attach(app)

        app.action_start_import()
        await settle(pilot, 4)
        await pilot.click("#import-ok")
        await settle(pilot)

        (destination / "2024/03/B.HEIC").unlink()
        app.action_rescan()
        await settle(pilot)
        assert app._status.unverified == 1

        app.action_start_delete()
        await settle(pilot, 8)
        assert app.screen.__class__.__name__ == "ConfirmDeleteScreen"
        dialog = " ".join(str(node.render()) for node in app.screen.query(Static))
        assert "local copy is missing or incomplete" in dialog

        await pilot.click("#confirm-input")
        await pilot.press(*CONFIRM_WORD)
        await pilot.click("#confirm-ok")
        await settle(pilot, 16)

        # Only the file with a good copy left the phone.
        assert backend.deletions == ["/DCIM/A.HEIC"]
        assert "/DCIM/B.HEIC" in backend.files


# ---------------------------------------------------------------------------
# 16. The delete dialog, without deleting
# ---------------------------------------------------------------------------


async def test_16_a_count_larger_than_the_library_is_clamped(destination: Path) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(100, 45)) as pilot:
        await settle(pilot, 3)
        await _import_two(app, pilot)

        app.action_start_delete()
        await settle(pilot, 8)
        await pilot.click("#scope-recent")
        app.screen.query_one("#confirm-count", Input).value = "99999"
        await settle(pilot, 3)

        summary = str(app.screen.query_one("#confirm-selection", Static).render())
        assert "only 2 are available" in summary
        assert "2 item(s)" in str(app.screen.query_one("#confirm-ok", Button).label)


async def test_16_escape_closes_the_delete_dialog_and_removes_nothing(
    destination: Path,
) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(100, 45)) as pilot:
        await settle(pilot, 3)
        backend = await _import_two(app, pilot)

        app.action_start_delete()
        await settle(pilot, 8)
        await pilot.click("#confirm-input")
        await pilot.press(*CONFIRM_WORD)  # armed, and still cancelled by Escape
        await pilot.press("escape")
        await settle(pilot)

        assert backend.deletions == []
        assert sorted(backend.files) == ["/DCIM/A.HEIC", "/DCIM/B.HEIC"]
        assert "cancelled" in activity_log(app).lower()


async def test_16_the_deep_check_is_on_by_default_and_can_be_turned_off(
    destination: Path,
) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(100, 45)) as pilot:
        await settle(pilot, 3)
        await _import_two(app, pilot)

        app.action_start_delete()
        await settle(pilot, 8)
        deep = app.screen.query_one("#confirm-deep", Checkbox)
        assert deep.value is True

        deep.value = False
        await settle(pilot, 3)
        assert app.screen.query_one("#confirm-deep", Checkbox).value is False


# ---------------------------------------------------------------------------
# 19. The app notices a library that changed underneath
# ---------------------------------------------------------------------------


async def test_19_leaving_the_history_alone_excludes_the_changed_files(
    destination: Path,
) -> None:
    """"Leave it as it is" keeps the rows -- and the delete gate must still refuse them."""
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(100, 45)) as pilot:
        await settle(pilot, 3)
        backend = await _import_two(app, pilot)

        # Erase and restore: same paths, different photos.
        backend.files["/DCIM/B.HEIC"] = make_file(
            "/DCIM/B.HEIC", b"newr", datetime(2026, 1, 6, 12, 0)
        )

        app.action_rescan()
        await settle(pilot, 10)
        assert app.screen.__class__.__name__ == "LibraryChangedScreen"

        await pilot.click("#changed-keep")
        await settle(pilot)
        assert "left as it is" in activity_log(app)
        assert app._database is not None
        assert len(app._database.live_records(backend.serial)) == 2

        app.action_start_delete()
        await settle(pilot, 8)
        dialog = " ".join(str(node.render()) for node in app.screen.query(Static))
        assert "no longer the ones that were imported" in dialog

        await pilot.click("#confirm-input")
        await pilot.press(*CONFIRM_WORD)
        await pilot.click("#confirm-ok")
        await settle(pilot, 16)

        assert backend.deletions == ["/DCIM/A.HEIC"]
        assert "/DCIM/B.HEIC" in backend.files


# ---------------------------------------------------------------------------
# 20. Your own archive is safe
# ---------------------------------------------------------------------------


async def test_20_files_the_app_did_not_create_are_untouched_and_untracked(
    destination: Path,
) -> None:
    stranger = destination / "2019" / "07" / "scan_holidays.jpg"
    stranger.parent.mkdir(parents=True)
    stranger.write_text("not from the phone")
    note = destination / "notes.txt"
    note.write_text("mine")

    app = IpmApp(config_for(destination))
    async with app.run_test(size=(100, 45)) as pilot:
        await settle(pilot, 3)
        backend = await _import_two(app, pilot)

        assert stranger.read_text() == "not from the phone"
        assert note.read_text() == "mine"

        assert app._database is not None
        tracked = {row.local_path for row in app._database.live_records(backend.serial)}
        assert not any("scan_holidays" in path or "notes" in path for path in tracked)

        # And what the app cannot see, it can never offer to delete.
        app.action_start_delete()
        await settle(pilot, 8)
        summary = str(app.screen.query_one("#confirm-summary", Static).render())
        assert "2 item(s)" in summary
        await pilot.press("escape")
        await settle(pilot, 3)


# ---------------------------------------------------------------------------
# 21. Two folders, one phone
# ---------------------------------------------------------------------------


async def test_21_a_second_folder_starts_empty_and_the_first_one_comes_back(
    destination: Path, tmp_path: Path
) -> None:
    other = tmp_path / "second-library"

    app = IpmApp(config_for(destination))
    async with app.run_test(size=(100, 45)) as pilot:
        await settle(pilot, 3)
        await _import_two(app, pilot)
        assert app._status.verified == 2

        await _choose_folder(app, pilot, other)
        assert app.config.destination == other
        assert app._status.verified == 0
        # Nothing imported here, so there is nothing this folder could delete.
        assert app.query_one("#delete-button", Button).disabled is True

        await _choose_folder(app, pilot, destination)
        assert app._status.verified == 2
        assert app.query_one("#delete-button", Button).disabled is False


# ---------------------------------------------------------------------------
# The permanence warning (no checklist number: it is new in this release)
# ---------------------------------------------------------------------------


async def test_the_first_delete_warns_that_there_is_no_bin(destination: Path) -> None:
    """It appears after the confirmation, and backing out of it deletes nothing.

    Last gate rather than first: warning about the consequences of a decision the
    user has not taken yet is noise, and it used to ask for the word ``DELETE``
    twice for one deletion.
    """
    app = IpmApp(config_for(destination, warning_seen=False))
    async with app.run_test(size=(100, 45)) as pilot:
        await settle(pilot, 3)
        backend = await _import_two(app, pilot)

        app.action_start_delete()
        await settle(pilot, 8)
        await _past_the_confirmation(pilot)
        assert app.screen.__class__.__name__ == "PermanentDeleteScreen"
        # The dialog exists to correct an expectation, so it has to name the thing
        # that is not true rather than only say "permanent".
        text = " ".join(str(node.render()) for node in app.screen.query(Static))
        assert "Recently Deleted" in text

        await pilot.press("escape")
        await settle(pilot, 6)
        assert backend.deletions == []
        assert app.config.permanent_delete_warning_seen is False


async def test_the_warning_asks_for_the_word_only_once_per_deletion(
    destination: Path,
) -> None:
    """The permanence notice does not ask for ``DELETE`` a second time.

    It used to, sitting before the confirmation, so one deletion meant typing the
    word twice. Typing it twice two seconds apart teaches people to type it without
    reading, which is the opposite of what the word is for.
    """
    app = IpmApp(config_for(destination, warning_seen=False))
    async with app.run_test(size=(100, 45)) as pilot:
        await settle(pilot, 3)
        backend = await _import_two(app, pilot)

        app.action_start_delete()
        await settle(pilot, 8)
        await _past_the_confirmation(pilot)

        assert app.screen.__class__.__name__ == "PermanentDeleteScreen"
        assert not app.screen.query("#permanent-input")
        # The red button is ready, but the focus is on Cancel: a stray Return on a
        # dialog that has just appeared must not delete anything.
        assert app.screen.query_one("#permanent-ok", Button).disabled is False
        assert app.screen.focused is app.screen.query_one("#permanent-cancel", Button)

        await pilot.click("#permanent-ok")
        await settle(pilot, 16)
        assert backend.deletions


async def test_the_warning_stays_away_once_the_box_is_ticked(
    destination: Path,
) -> None:
    """Ticking it is remembered in the settings file, not just for this session."""
    app = IpmApp(config_for(destination, warning_seen=False))
    async with app.run_test(size=(100, 45)) as pilot:
        await settle(pilot, 3)
        backend = await _import_two(app, pilot)

        app.action_start_delete()
        await settle(pilot, 8)
        await _past_the_confirmation(pilot)
        app.screen.query_one("#permanent-dismiss", Checkbox).value = True
        await pilot.press("escape")
        await settle(pilot, 6)

        # Backing out still deletes nothing -- but the notice was about the notice,
        # so the choice not to see it again is kept either way.
        assert backend.deletions == []
        assert app.config.permanent_delete_warning_seen is True
        assert load_config().permanent_delete_warning_seen is True

    # A second run of the application does not ask again: past the confirmation the
    # deletion simply happens.
    app2 = IpmApp(load_config())
    async with app2.run_test(size=(100, 45)) as pilot:
        await settle(pilot, 3)
        # Already imported into that folder, so this run only has to attach.
        backend2 = await attach(app2)
        app2.action_rescan()
        await settle(pilot, 8)
        app2.action_start_delete()
        await settle(pilot, 8)
        await _past_the_confirmation(pilot)
        assert app2.screen.__class__.__name__ != "PermanentDeleteScreen"
        await settle(pilot, 16)
        assert backend2.deletions


async def test_a_warning_that_was_never_dismissed_comes_back(destination: Path) -> None:
    app = IpmApp(config_for(destination, warning_seen=False))
    async with app.run_test(size=(100, 45)) as pilot:
        await settle(pilot, 3)
        backend = await _import_two(app, pilot)

        app.action_start_delete()
        await settle(pilot, 8)
        await _past_the_confirmation(pilot)
        assert app.screen.__class__.__name__ == "PermanentDeleteScreen"
        await pilot.press("escape")  # backed out, and the box was never ticked
        await settle(pilot, 6)
        assert backend.deletions == []
        assert app.config.permanent_delete_warning_seen is False

        app.action_start_delete()
        await settle(pilot, 8)
        await _past_the_confirmation(pilot)
        assert app.screen.__class__.__name__ == "PermanentDeleteScreen"
        await pilot.press("escape")
        await settle(pilot, 3)


# ---------------------------------------------------------------------------
# The "On this Mac" panel (no checklist number: new in this release)
# ---------------------------------------------------------------------------


async def test_the_two_panels_end_up_the_same_height(destination: Path) -> None:
    """They sit side by side, so an uneven pair reads as a rendering fault.

    Matched by measurement rather than by counting lines, because a line longer
    than the panel is wide takes two rows -- which is why this asserts on the
    rendered height and not on the text.
    """
    # Wide enough that the panels are side by side at all: below NARROW_WIDTH they
    # stack, and stacked they are deliberately *not* matched in height.
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot, 3)
        await attach(app)
        app._render_all()
        await settle(pilot, 4)

        left = app.query_one("#device-card")
        right = app.query_one("#library-card")
        assert not app._narrow
        assert left.region.height == right.region.height
        assert left.region.height > 0


async def test_the_mac_panel_says_how_much_room_is_left(destination: Path) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(100, 40)) as pilot:
        await settle(pilot, 4)
        panel = str(app.query_one("#library-body", Static).render())
        assert "Disk" in panel
        assert "free of" in panel


async def test_the_mac_panel_says_what_is_still_to_import(destination: Path) -> None:
    """The number someone actually wants before starting a long transfer."""
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(100, 40)) as pilot:
        await settle(pilot, 3)
        await attach(app)  # two files on the phone, nothing imported yet
        app.action_rescan()
        await settle(pilot, 8)

        panel = str(app.query_one("#library-body", Static).render())
        assert "To import" in panel
        assert app._status.pending_files == 2
        # How many files are on the phone is the other panel's job; saying it twice
        # would cost a line to repeat what is already a centimetre away.
        assert "On the iPhone" not in panel

        app.action_start_import()
        await settle(pilot, 4)
        await pilot.click("#import-ok")
        await settle(pilot)

        panel = str(app.query_one("#library-body", Static).render())
        assert "Everything on the iPhone is in this folder" in panel
        assert app._status.pending_files == 0


async def test_the_mac_panel_splits_what_was_imported_into_photos_and_videos(
    destination: Path,
) -> None:
    """The split describes the folder, not the phone -- and a Live Photo is one photo."""
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(110, 40)) as pilot:
        await settle(pilot, 3)
        await attach(
            app,
            make_file("/DCIM/A.HEIC", b"aaa", datetime(2024, 3, 1, 9, 0)),
            make_file("/DCIM/A.MOV", b"aaaaaa", datetime(2024, 3, 1, 9, 0)),
            make_file("/DCIM/B.MP4", b"bbbb", datetime(2024, 3, 2, 9, 0)),
        )
        # Nothing imported yet, so there is nothing to break down.
        app.action_rescan()
        await settle(pilot, 8)
        assert "photos ·" not in str(app.query_one("#library-body", Static).render())

        app.action_start_import()
        await settle(pilot, 4)
        await pilot.click("#import-ok")
        await settle(pilot)

        panel = str(app.query_one("#library-body", Static).render())
        assert "1 photos · 1 videos" in panel
        assert app._status.photos == 1
        assert app._status.videos == 1
        # Three files, two items: the Live Photo half is not a photo of its own.
        assert app._status.verified == 3
        assert app._status.verified_items == 2

        # And it is the Mac panel that says it, not the phone one.
        assert "photos ·" not in str(app.query_one("#device-body", Static).render())


async def test_the_coverage_lines_stay_away_when_no_phone_is_attached(
    destination: Path,
) -> None:
    """A scan from an hour ago must not be shown as if it were current."""
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(100, 40)) as pilot:
        await settle(pilot, 3)
        backend = await attach(app)
        app.action_rescan()
        await settle(pilot, 8)
        assert "To import" in str(app.query_one("#library-body", Static).render())

        await app._handle_disconnect(backend.serial)
        await settle(pilot, 6)

        assert "To import" not in str(app.query_one("#library-body", Static).render())


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _past_the_confirmation(pilot: Pilot[None]) -> None:
    """Accept the delete dialog, which is what now brings up the permanence warning.

    The warning is the last gate, not the first: these tests have to walk through
    the confirmation to reach it.
    """
    await pilot.click("#confirm-input")
    await pilot.press(*CONFIRM_WORD)
    await pilot.click("#confirm-ok")
    await settle(pilot, 8)


async def _import_two(app: IpmApp, pilot: Pilot[None]) -> FakeBackend:
    """Attach the default fake phone and import both of its files."""
    backend = await attach(app)
    app.action_start_import()
    await settle(pilot, 4)
    await pilot.click("#import-ok")
    await settle(pilot)
    return backend


async def _choose_folder(app: IpmApp, pilot: Pilot[None], folder: Path) -> None:
    """Drive the destination dialog the way pressing ``f`` does."""
    app.action_change_folder()
    await settle(pilot, 4)
    app.screen.query_one("#destination-input", Input).value = str(folder)
    await pilot.click("#destination-ok")
    await settle(pilot)


# ---------------------------------------------------------------------------
# Stopping a running job, and the run log (new in this release)
# ---------------------------------------------------------------------------


async def test_a_running_job_can_be_stopped(destination: Path) -> None:
    """Nothing else offered a way out of a sixteen-minute transfer.

    What the flag then does -- finish the current file, promote or discard the
    ``.ipm-part``, leave nothing half-written -- belongs to the importer, and
    ``test_importer.py`` pins it there. This is about the key reaching the flag.
    """
    app = IpmApp(config_for(destination))
    async with app.run_test() as pilot:
        await settle(pilot, 3)
        cancel = asyncio.Event()
        app._busy = True
        app._cancel = cancel

        app.action_stop()
        await pilot.pause()
        assert cancel.is_set()
        assert "Stopping" in activity_log(app)

        # Pressing it again says so rather than pretending something new happened.
        app.action_stop()
        await pilot.pause()
        assert "Already stopping" in activity_log(app)

        app._busy = False


async def test_stopping_when_nothing_runs_does_nothing(destination: Path) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test() as pilot:
        await settle(pilot, 3)
        app.action_stop()
        await pilot.pause()
        assert "Stopping" not in activity_log(app)


async def test_quitting_during_a_job_asks_once(destination: Path) -> None:
    """A transfer is long and q is easy to hit; the second press means it."""
    app = IpmApp(config_for(destination))
    async with app.run_test() as pilot:
        await settle(pilot, 3)
        app._busy = True

        await app.action_quit()
        await pilot.pause()
        assert "q" in activity_log(app) and "again to quit" in activity_log(app)
        assert app.is_running

        app._busy = False


async def test_every_line_on_screen_is_also_written_to_the_run_log(
    destination: Path,
) -> None:
    """The file a bug report can be built from, since the panel goes with the app."""
    app = IpmApp(config_for(destination))
    async with app.run_test() as pilot:
        await settle(pilot, 3)
        app._activity("[green]something happened[/green]")
        await pilot.pause()

        log = destination / ".ipm" / "last-run.log"
        assert log.exists()
        written = log.read_text(encoding="utf-8")
        # Plain text: the file is for pasting into an issue, not for rendering.
        assert "something happened" in written
        assert "[green]" not in written
        assert str(destination) in written  # the header says which folder


async def test_the_previous_run_is_kept(destination: Path) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test() as pilot:
        await settle(pilot, 3)
        app._activity("first run")
        await pilot.pause()

    app2 = IpmApp(config_for(destination))
    async with app2.run_test() as pilot:
        await settle(pilot, 3)
        app2._activity("second run")
        await pilot.pause()

    assert "second run" in (destination / ".ipm" / "last-run.log").read_text()
    assert "first run" in (destination / ".ipm" / "last-run.log.1").read_text()


async def test_the_activity_panel_is_labelled(destination: Path) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test() as pilot:
        await settle(pilot, 3)
        assert app.query_one("#activity").border_title == "Activity"


async def test_the_delete_button_says_which_way_the_photos_go(destination: Path) -> None:
    """Shortened, but not past the word that matters.

    "imported files" was a third of a row of terminal to say what the tooltip, the
    dialog and the panel beside it all say already. "from iPhone" is the part that
    cannot be dropped: it is the direction, and the direction is the risk.
    """
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(pilot, 3)
        label = str(app.query_one("#delete-button", Button).label)
        assert "from iPhone" in label
        assert len(label) <= 20


async def test_the_buttons_fit_an_eighty_column_terminal(destination: Path) -> None:
    """The width almost every terminal opens at, and where they used to run off."""
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(80, 30)) as pilot:
        await settle(pilot, 4)
        row = app.query_one("#buttons")
        buttons = list(app.query(Button))
        rightmost = max(button.region.right for button in buttons)
        assert rightmost <= row.region.right, "a button is off the right edge"


async def test_a_narrow_terminal_stacks_the_panels(destination: Path) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(80, 30)) as pilot:
        await settle(pilot, 4)
        assert app._narrow is True
        left = app.query_one("#device-card")
        right = app.query_one("#library-card")
        # Stacked, not side by side.
        assert right.region.y > left.region.y
        assert left.region.width == right.region.width

        await pilot.resize_terminal(120, 40)
        await settle(pilot, 4)
        assert app._narrow is False
        assert app.query_one("#library-card").region.y == app.query_one("#device-card").region.y


# ---------------------------------------------------------------------------
# Colours, and what happens when there are none
# ---------------------------------------------------------------------------


async def test_the_app_uses_the_apple_palette(destination: Path) -> None:
    """A macOS tool standing in for the Photos app, in macOS's own colours."""
    app = IpmApp(config_for(destination))
    async with app.run_test() as pilot:
        await settle(pilot, 3)
        assert app.theme == THEME_NAME
        # Registered, so ctrl+p can switch away from it and back.
        assert THEME_NAME in app.available_themes


async def test_severity_is_marked_as_well_as_coloured(destination: Path) -> None:
    """The colour is what you notice; the marker is what survives losing it."""
    app = IpmApp(config_for(destination))
    async with app.run_test() as pilot:
        await settle(pilot, 3)
        app._activity("[green]it worked[/green]")
        app._activity("[yellow]something was kept[/yellow]")
        app._activity("[red]it did not work[/red]")
        app._activity("just so you know")
        await pilot.pause()

        log = activity_log(app)
        assert "✓ it worked" in log
        assert "! something was kept" in log
        assert "✗ it did not work" in log
        # Ordinary lines get a blank column, so the markers stay in one column.
        assert "  just so you know" in log


async def test_the_run_log_carries_the_markers_too(destination: Path) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test() as pilot:
        await settle(pilot, 3)
        app._activity("[red]it did not work[/red]")
        await pilot.pause()

    written = (destination / ".ipm" / "last-run.log").read_text(encoding="utf-8")
    assert "✗ it did not work" in written


async def test_with_no_colour_an_error_is_the_loudest_line_not_the_faintest(
    destination: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NO_COLOR is honoured by Textual, and its filter converts colour to luminance.

    Red on a dark background has very little, so an error rendered through it comes
    out dimmer than ordinary text -- the opposite of what an error needs. The colour
    is dropped before the filter sees it, and the marker and the weight say it
    instead.
    """
    monkeypatch.setenv("NO_COLOR", "1")
    app = IpmApp(config_for(destination))
    async with app.run_test() as pilot:
        await settle(pilot, 3)
        assert app.no_color is True

        app._activity("[red]the iPhone is locked[/red]")
        await pilot.pause()

        line = next(
            line for line in activity_log(app).splitlines() if "iPhone is locked" in line
        )
        assert "✗" in line
        assert "[red]" not in line  # the markup went, not just the rendering


# ---------------------------------------------------------------------------
# The progress bar
# ---------------------------------------------------------------------------


async def test_no_bar_while_nothing_is_running(destination: Path) -> None:
    """A bar reading 0% when there is nothing to be 0% of says less than no bar."""
    app = IpmApp(config_for(destination))
    async with app.run_test() as pilot:
        await settle(pilot, 3)
        assert app.query_one("#progress", ProgressBar).visible is False

        app._begin_job("working…")
        await pilot.pause()
        assert app.query_one("#progress", ProgressBar).visible is True

        app._end_job("done")
        await pilot.pause()
        assert app.query_one("#progress", ProgressBar).visible is False


async def test_the_screen_does_not_jump_when_a_job_starts(destination: Path) -> None:
    """Hidden, not removed: the row stays reserved.

    ``display: none`` collapsed it, so the activity box moved up a line every time a
    job ended and back down when the next one began -- on a screen someone is
    watching for twenty minutes.
    """
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(120, 34)) as pilot:
        await settle(pilot, 3)
        idle = app.query_one("#activity").region

        app._begin_job("working…")
        await pilot.pause()
        assert app.query_one("#activity").region == idle

        app._end_job("done")
        await pilot.pause()
        assert app.query_one("#activity").region == idle


async def test_a_job_that_cannot_say_how_long_it_will_be_pulses(destination: Path) -> None:
    """Cataloguing the library takes twenty seconds and only knows what it has found.

    A bar sitting at 0% for that long is indistinguishable from one that has hung,
    so it starts indeterminate and becomes a real percentage as soon as some phase
    knows its total.
    """
    app = IpmApp(config_for(destination))
    async with app.run_test() as pilot:
        await settle(pilot, 3)
        app._begin_job("Reading the iPhone's photo library…")
        await pilot.pause()

        bar = app.query_one("#progress", ProgressBar)
        assert bar.total is None
        assert bar.percentage is None  # nothing to report yet, and it says so

        bar.update(total=16932, progress=1204)
        await pilot.pause()
        assert bar.percentage is not None

        app._end_job("done")
