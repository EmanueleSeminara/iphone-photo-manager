"""Photos edited on the phone are imported but never deleted, for now.

iOS keeps an edit apart from the original: editing ``/DCIM/117APPLE/IMG_7130.HEIC``
writes ``/PhotoData/Mutations/DCIM/117APPLE/IMG_7130/Adjustments/FullSizeRender.heic``
and leaves the original alone. This application imports the original only, so
deleting the item from the phone would lose the edit. Until edits are imported too,
every file of an edited item -- still, Live Photo half, sidecar -- stays on the
phone, and the delete dialog says so instead of hiding them.

The tests run at three levels: the device walk that finds the edits (stub AFC), the
deletion rules (fake phone), and the whole application (Pilot).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from textual.widgets import Static

from ipm.core.database import ManifestDatabase
from ipm.core.deleter import Deleter, collect_candidates
from ipm.core.importer import Importer
from ipm.device.afc import MUTATIONS_ROOT, AfcDeviceBackend
from ipm.tui.app import IpmApp
from ipm.tui.screens import CONFIRM_WORD
from tests.fakes import FakeAssetService, FakeBackend, make_file
from tests.test_afc_listing import StubAfc, directory, regular
from tests.tui_harness import (  # noqa: F401 - the three fixtures are autouse
    activity_log,
    attach,
    config_for,
    no_finder,
    no_real_devices,
    no_real_photo_library,
    settle,
)

MARCH = datetime(2024, 3, 14, 9, 0, 0)
EDITED = "/DCIM/117APPLE/IMG_7130"
"""Item key of the photo the tests edit: the still, its Live Photo half."""


def _log(app: IpmApp) -> str:
    """The activity log with its line wrapping undone, for phrase matching."""
    return " ".join(activity_log(app).split())


def _phone() -> FakeBackend:
    """An edited Live Photo and an untouched photo, side by side."""
    backend = FakeBackend.with_files(
        make_file(f"{EDITED}.HEIC", b"still", MARCH),
        make_file(f"{EDITED}.MOV", b"motion", MARCH),
        make_file("/DCIM/117APPLE/IMG_7131.HEIC", b"plain", MARCH),
    )
    backend.edited = {EDITED}
    return backend


# ---------------------------------------------------------------------------
# Finding the edits on the device
# ---------------------------------------------------------------------------


def _mutations(tree: dict[str, list[str]], stats: dict[str, dict[str, Any]]) -> AfcDeviceBackend:
    return AfcDeviceBackend(lockdown=None, afc=StubAfc(tree, stats), serial="STUB")  # type: ignore[arg-type]


def _edit_tree(*names: str) -> tuple[dict[str, list[str]], dict[str, dict[str, Any]]]:
    """A Mutations tree holding a rendered edit for each of *names* (``IMG_7130``)."""
    root = MUTATIONS_ROOT
    tree: dict[str, list[str]] = {root: ["DCIM"], f"{root}/DCIM": ["117APPLE"]}
    stats: dict[str, dict[str, Any]] = {
        root: directory(),
        f"{root}/DCIM": directory(),
        f"{root}/DCIM/117APPLE": directory(),
    }
    tree[f"{root}/DCIM/117APPLE"] = list(names)
    for name in names:
        item = f"{root}/DCIM/117APPLE/{name}"
        tree[item] = ["Adjustments"]
        tree[f"{item}/Adjustments"] = ["Adjustments.plist", "FullSizeRender.heic"]
        stats[item] = directory()
        stats[f"{item}/Adjustments"] = directory()
        stats[f"{item}/Adjustments/Adjustments.plist"] = regular(1003)
        stats[f"{item}/Adjustments/FullSizeRender.heic"] = regular(2_021_921)
    return tree, stats


async def test_the_path_of_an_edit_names_the_photo_it_belongs_to() -> None:
    backend = _mutations(*_edit_tree("IMG_7130", "IMG_7131"))
    edited = await backend.list_edited_items()
    assert EDITED in edited
    assert "/DCIM/117APPLE/IMG_7131" in edited
    # Only the folders above the renders: nothing a real item key could collide with.
    assert edited == {
        "/DCIM",
        "/DCIM/117APPLE",
        EDITED,
        f"{EDITED}/Adjustments",
        "/DCIM/117APPLE/IMG_7131",
        "/DCIM/117APPLE/IMG_7131/Adjustments",
    }


async def test_a_phone_that_was_never_edited_has_no_edits() -> None:
    backend = _mutations({}, {})
    assert await backend.list_edited_items() == set()


async def test_a_folder_with_nothing_in_it_is_not_an_edit() -> None:
    tree, stats = _edit_tree("IMG_7130")
    tree[f"{MUTATIONS_ROOT}/DCIM/117APPLE"].append("IMG_7140")
    tree[f"{MUTATIONS_ROOT}/DCIM/117APPLE/IMG_7140"] = ["Adjustments"]
    tree[f"{MUTATIONS_ROOT}/DCIM/117APPLE/IMG_7140/Adjustments"] = []
    stats[f"{MUTATIONS_ROOT}/DCIM/117APPLE/IMG_7140"] = directory()
    stats[f"{MUTATIONS_ROOT}/DCIM/117APPLE/IMG_7140/Adjustments"] = directory()

    edited = await _mutations(tree, stats).list_edited_items()
    assert EDITED in edited
    assert "/DCIM/117APPLE/IMG_7140" not in edited


async def test_an_edit_that_vanishes_mid_walk_is_skipped() -> None:
    tree, stats = _edit_tree("IMG_7130", "IMG_7131")
    del stats[f"{MUTATIONS_ROOT}/DCIM/117APPLE/IMG_7131"]
    edited = await _mutations(tree, stats).list_edited_items()
    assert EDITED in edited
    assert "/DCIM/117APPLE/IMG_7131" not in edited


async def test_a_folder_that_cannot_be_read_fails_the_whole_answer() -> None:
    """A half-read tree must never pass for "these are all the edits"."""

    class Unreadable(StubAfc):
        async def listdir(self, path: str) -> list[str]:
            if path.endswith("117APPLE"):
                raise PermissionError(path)
            return await super().listdir(path)

    tree, stats = _edit_tree("IMG_7130")
    backend = AfcDeviceBackend(lockdown=None, afc=Unreadable(tree, stats), serial="STUB")  # type: ignore[arg-type]
    with pytest.raises(PermissionError):
        await backend.list_edited_items()


async def test_the_media_scan_still_leaves_the_edits_out() -> None:
    """Finding the edits is a separate walk; the renders are not library items."""
    tree, stats = _edit_tree("IMG_7130")
    files = await _mutations(tree, stats).list_media()
    assert files == []


# ---------------------------------------------------------------------------
# The deletion rules
# ---------------------------------------------------------------------------


async def _import(backend: FakeBackend, database: ManifestDatabase, destination: Path) -> None:
    await Importer(backend, database, destination).run()


async def test_an_edited_photo_is_imported_like_any_other(
    database: ManifestDatabase, destination: Path
) -> None:
    backend = _phone()
    await _import(backend, database, destination)
    assert (destination / "2024/03/IMG_7130.HEIC").read_bytes() == b"still"
    assert (destination / "2024/03/IMG_7130.MOV").read_bytes() == b"motion"


async def test_every_file_of_an_edited_item_is_held_back(
    database: ManifestDatabase, destination: Path
) -> None:
    backend = _phone()
    await _import(backend, database, destination)

    candidates = collect_candidates(
        database, destination, backend.serial, await backend.list_media(), backend.edited
    )
    assert [record.device_path for record in candidates.deletable] == [
        "/DCIM/117APPLE/IMG_7131.HEIC"
    ]
    assert sorted(record.device_path for record in candidates.edited_on_device) == [
        f"{EDITED}.HEIC",
        f"{EDITED}.MOV",
    ]
    assert candidates.held_back == 2


async def test_deleting_everything_leaves_the_edited_photo_on_the_phone(
    database: ManifestDatabase, destination: Path
) -> None:
    backend = _phone()
    await _import(backend, database, destination)
    assets = FakeAssetService(backend)
    deleter = Deleter(
        backend,
        database,
        destination,
        assets=assets,
        device_files=await backend.list_media(),
        edited_items=backend.edited,
    )

    stats = await deleter.run()

    assert assets.requested == ["/DCIM/117APPLE/IMG_7131.HEIC"]
    assert backend.deletions == ["/DCIM/117APPLE/IMG_7131.HEIC"]
    assert f"{EDITED}.HEIC" in backend.files
    assert f"{EDITED}.MOV" in backend.files
    assert stats.items_deleted == 1


async def test_the_last_gate_refuses_an_edited_photo_whatever_list_it_came_from(
    database: ManifestDatabase, destination: Path
) -> None:
    """Handed the edited rows directly, the run still keeps them and says why."""
    backend = _phone()
    await _import(backend, database, destination)
    assets = FakeAssetService(backend)
    deleter = Deleter(
        backend,
        database,
        destination,
        assets=assets,
        device_files=await backend.list_media(),
        edited_items=backend.edited,
    )
    everything = database.live_records(backend.serial)

    stats = await deleter.run(everything)

    assert f"{EDITED}.HEIC" in backend.files
    assert f"{EDITED}.MOV" in backend.files
    assert assets.requested == ["/DCIM/117APPLE/IMG_7131.HEIC"]
    assert sum("edited on the iPhone" in message for message in stats.errors) == 2


# ---------------------------------------------------------------------------
# The application
# ---------------------------------------------------------------------------


async def test_the_dialog_shows_the_edited_photos_and_keeps_them(destination: Path) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(100, 50)) as pilot:
        await settle(pilot, 3)
        backend = await attach(app, *_phone().files.values())
        backend.edited = {EDITED}

        app.action_start_import()
        await settle(pilot, 4)
        await pilot.click("#import-ok")
        await settle(pilot)

        app.action_rescan()
        await settle(pilot)
        assert "1 of them were edited on the iPhone" in _log(app)

        app.action_start_delete()
        await settle(pilot, 8)
        assert app.screen.__class__.__name__ == "ConfirmDeleteScreen"
        dialog = " ".join(str(node.render()) for node in app.screen.query(Static))
        assert "edited on the iPhone" in dialog
        assert "Support is on the way" in dialog
        assert "IMG_7130.HEIC" in dialog

        await pilot.click("#confirm-input")
        await pilot.press(*CONFIRM_WORD)
        await pilot.click("#confirm-ok")
        await settle(pilot, 16)

        assert backend.deletions == ["/DCIM/117APPLE/IMG_7131.HEIC"]
        assert f"{EDITED}.HEIC" in backend.files
        assert f"{EDITED}.MOV" in backend.files


async def test_nothing_is_deleted_while_the_edits_cannot_be_read(destination: Path) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(100, 50)) as pilot:
        await settle(pilot, 3)
        backend = await attach(app, *_phone().files.values())

        app.action_start_import()
        await settle(pilot, 4)
        await pilot.click("#import-ok")
        await settle(pilot)

        backend.fail_edits = True
        app.action_rescan()
        await settle(pilot)
        assert "deleting is off until a rescan succeeds" in _log(app)

        app.action_start_delete()
        await settle(pilot, 10)
        assert app.screen.__class__.__name__ != "ConfirmDeleteScreen"
        assert backend.deletions == []


async def test_a_phone_holding_only_edited_photos_says_why_nothing_can_go(
    destination: Path,
) -> None:
    app = IpmApp(config_for(destination))
    async with app.run_test(size=(100, 50)) as pilot:
        await settle(pilot, 3)
        backend = await attach(
            app,
            make_file(f"{EDITED}.HEIC", b"still", MARCH),
            make_file(f"{EDITED}.MOV", b"motion", MARCH),
        )
        backend.edited = {EDITED}

        app.action_start_import()
        await settle(pilot, 4)
        await pilot.click("#import-ok")
        await settle(pilot)
        app.action_rescan()
        await settle(pilot)

        app.action_start_delete()
        await settle(pilot, 8)
        assert app.screen.__class__.__name__ != "ConfirmDeleteScreen"
        assert "was edited there" in _log(app)
        assert backend.deletions == []
