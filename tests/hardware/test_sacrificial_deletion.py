"""The only automated test in this project that removes a photo from a phone.

It is check 17 of ``MANUAL_TESTING.md``, scripted: delete one item, then measure
what actually left the device. The measurement is the point. Nobody has yet
watched an iPhone delete a library item and seen whether the Live Photo's ``.MOV``
half and the ``.AAE`` sidecar go with it, and the whole design assumes they
do.

**Nothing here chooses what to delete.** The list comes from a file you write by
hand, `tests/hardware/sacrificial.txt`, and an item that is not named in it cannot
be touched -- there is no "most recent N" and no default. That inversion is the
single most important property of this module: a bug in the selection logic cannot
reach a photo you care about, because the selection is not logic, it is your file.

Before anything is requested, every named file is checked independently of the
application's own gate: it must exist on the phone, have a manifest row, and have
a local copy whose SHA-256 is recomputed here and compared with the recorded one.
If the application's gate ever breaks, this test still refuses.

Run it, deliberately, with:

    pytest -m hardware --allow-delete -q tests/hardware/test_sacrificial_deletion.py

**Photos removed this way do not go to *Recently Deleted*.** Measured on an iPhone 13
running iOS 26.6: the photo service removes items outright, unlike deleting in the
Photos app. There is no safety net here -- the copy in the destination folder is the
only one that survives, which is why nothing is deleted until its checksum has just
been recomputed and matched.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ipm.core.database import ManifestDatabase, manifest_path_for
from ipm.core.deleter import Deleter, collect_candidates
from ipm.core.digest import digest_of
from ipm.core.items import ItemGroup, group_by_item
from ipm.device.afc import AfcDeviceBackend
from ipm.device.ptp import PtpService
from ipm.models import ImportRecord, RemoteFile
from tests.hardware.conftest import HardwareConfig

pytestmark = pytest.mark.hardware

SACRIFICIAL_FILE = Path(__file__).with_name("sacrificial.txt")

MAX_ITEMS = 3
"""Hard ceiling on how many items one run may remove.

Not configurable, on purpose. A destructive test that can be widened by a setting
is a destructive test that will one day be widened by a typo.
"""


def _offered() -> set[str]:
    """What the tester has explicitly offered up, from the list file.

    Entries are device paths (``/DCIM/117APPLE/IMG_7045.JPG``) or bare file names.
    A bare name is a convenience, not a shortcut: it is resolved against the phone
    and refused if it matches more than one file -- see :func:`_resolve`.
    """
    if not SACRIFICIAL_FILE.is_file():
        pytest.skip(
            f"no {SACRIFICIAL_FILE.name}: write one device path per line (for example "
            "/DCIM/117APPLE/IMG_7045.JPG) naming photos you are willing to lose. "
            "Nothing is deleted without it."
        )
    entries = {
        line.strip()
        for line in SACRIFICIAL_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    if not entries:
        pytest.skip(f"{SACRIFICIAL_FILE.name} is empty; nothing has been offered up")
    return entries


def _resolve(entries: set[str], records: list[ImportRecord]) -> set[str]:
    """Turn the offered entries into exact device paths, or refuse to guess.

    Three ways to name a photo, in the order they are recognised:

    * a **local path**, ``2026/09/IMG_7045.JPG`` -- what you see in the Finder, and
      the one to prefer. It is unique inside a destination folder;
    * a **device path**, ``/DCIM/117APPLE/IMG_7045.JPG`` -- exact, but you have to
      go and look it up;
    * a **bare file name**, ``IMG_7045.JPG`` -- a convenience, and only when it is
      unambiguous.

    That last one is why this function exists. **File names on an iPhone are not
    unique**: iOS numbers photos per folder and starts again in the next one, so
    ``IMG_7045.JPG`` can be a photo from last year *and* the throwaway you took a
    minute ago. A name matching two files stops the run and prints both, because
    picking one would be guessing which photo the tester was willing to lose.

    :param entries: Lines from the sacrificial list.
    :param records: Manifest rows for this phone, which hold both paths.
    :return: Exact device paths, one per entry.
    """
    by_local = {record.local_path: record.device_path for record in records}
    by_device = {record.device_path for record in records}
    by_name: dict[str, list[str]] = {}
    for record in records:
        by_name.setdefault(record.local_path.rsplit("/", 1)[-1], []).append(record.device_path)

    resolved: set[str] = set()
    for entry in sorted(entries):
        normalised = entry.strip().lstrip("./")
        if entry.startswith("/"):
            if entry not in by_device:
                pytest.fail(
                    f"{entry} is not an imported file on this phone. Device paths look "
                    "like /DCIM/117APPLE/IMG_7045.JPG."
                )
            resolved.add(entry)
        elif "/" in normalised:
            if normalised not in by_local:
                pytest.fail(
                    f"{normalised} is not in this folder's import history. Local paths "
                    "look like 2026/09/IMG_7045.JPG, relative to the destination."
                )
            resolved.add(by_local[normalised])
        else:
            matches = sorted(set(by_name.get(normalised, [])))
            if not matches:
                pytest.fail(f"no imported file is called {normalised}")
            if len(matches) > 1:
                pytest.fail(
                    f"{normalised} is ambiguous: {len(matches)} imported files have that "
                    f"name -- {matches}. Use the local path (2026/09/{normalised}) so "
                    "there is nothing to guess."
                )
            resolved.add(matches[0])
    return resolved


def _armed(config: HardwareConfig) -> None:
    """Refuse, loudly, unless every gate has been opened deliberately.

    Not giving ``--allow-delete`` is a skip: it is the normal state, and a red run
    every time someone checks the rest of the suite would train them to ignore red.
    Everything else is a failure, because it means the settings disagree about which
    phone may lose photos, and that is not a thing to pass over quietly.
    """
    if not config.allow_delete:
        pytest.skip("--allow-delete was not given; this test removes photos from a phone")
    if config.is_protected:
        pytest.fail(
            f"the iPhone {config.udid} is on IPM_HW_PROTECTED_UDIDS. That list overrides "
            "--allow-delete: take it off that list if you really mean to remove photos here."
        )
    if not config.may_delete:
        pytest.fail(f"refusing to delete: {config.refusal()}")
    config.require_destination()


def _groups_for(paths: set[str], records: list[ImportRecord]) -> list[ItemGroup]:
    """Expand the offered device paths into whole items.

    You name a photo; what goes is the item it belongs to, which is what iOS
    deletes -- the still, its Live Photo half and its sidecar together. Naming one
    half of an item and expecting the other to survive is not a thing the phone
    offers, so the test does not pretend otherwise.

    Matching is on the full device path, never the file name: two folders on the
    same phone can hold an ``IMG_7045.JPG`` each.
    """
    chosen: list[ItemGroup] = []
    for group in group_by_item(records):
        if {record.device_path for record in group.records} & paths:
            chosen.append(group)
    return chosen


async def test_one_named_item_is_deleted_and_the_rest_of_the_phone_is_not(
    hw_config: HardwareConfig,
    backend: AfcDeviceBackend,
    media: list[RemoteFile],
) -> None:
    """Delete what was offered up, then measure what really went."""
    _armed(hw_config)
    entries = _offered()
    destination = hw_config.destination
    assert destination is not None

    manifest_file = manifest_path_for(destination)
    if not manifest_file.exists():
        pytest.fail(f"no manifest at {manifest_file}: import into that folder first")

    database = ManifestDatabase(manifest_file)
    try:
        # Resolved against the whole import history, not just what is deletable, so
        # "you never imported that" and "that one is not safe to delete" stay
        # different sentences.
        live = await asyncio.to_thread(database.live_records, hw_config.udid)
        offered = _resolve(entries, list(live))

        candidates = await asyncio.to_thread(
            collect_candidates, database, destination, hw_config.udid, media
        )
        groups = _groups_for(offered, list(candidates.deletable))
        reached = {record.device_path for group in groups for record in group.records}
        unreachable = sorted(offered - reached)
        if unreachable:
            pytest.fail(
                f"{unreachable} is imported but not currently deletable: the local copy "
                "may be missing or the file on the phone may have changed. Press d in "
                "the app to see the reason it is being kept."
            )
        assert len(groups) <= MAX_ITEMS, (
            f"{len(groups)} items matched the list, and the ceiling is {MAX_ITEMS}. "
            "Shorten sacrificial.txt."
        )

        selection = [record for group in groups for record in group.records]

        # -- the independent pre-flight ---------------------------------------
        # The application has its own gate, and it runs again inside Deleter.run().
        # This one exists so that a bug in that gate cannot disable the check that
        # would have caught it.
        on_device = {entry.path: entry.size for entry in media}
        for record in selection:
            local = destination / record.local_path
            assert local.is_file(), f"{record.local_path} has no local copy"
            assert local.stat().st_size == record.size, f"{record.local_path} is the wrong size"
            assert record.device_path in on_device, f"{record.device_path} is not on the phone"
            assert on_device[record.device_path] == record.size
            assert record.sha256, f"{record.local_path} has no recorded digest to check against"
            recomputed = await asyncio.to_thread(digest_of, local)
            assert recomputed == record.sha256, (
                f"the local copy of {record.local_path} does not match what was imported; "
                "refusing to delete the original"
            )

        doomed = {record.device_path for record in selection}
        before = {entry.path for entry in media}
        local_before = {
            record.local_path: (destination / record.local_path).stat().st_size
            for record in selection
        }

        # -- the deletion ------------------------------------------------------
        service = PtpService()
        try:
            await service.connect()
            deleter = Deleter(
                backend, database, destination, assets=service, device_files=media
            )
            stats = await deleter.run(selection)
        finally:
            await service.aclose()

        # -- what actually happened -------------------------------------------
        after = {entry.path for entry in await backend.list_media()}
        vanished = before - after

        # The assertion that matters most: nothing outside the offered items went.
        collateral = sorted(vanished - doomed)
        assert not collateral, (
            "files disappeared from the iPhone that were never offered up: "
            f"{collateral[:10]}"
        )

        # The local copies are untouched -- deletion happens on the phone only.
        for local_path, size in local_before.items():
            copy = destination / local_path
            assert copy.is_file(), f"{local_path} disappeared from the Mac"
            assert copy.stat().st_size == size

        assert stats.items_deleted == len(groups), (
            f"asked for {len(groups)} item(s), the phone confirmed {stats.items_deleted}. "
            f"Errors: {stats.errors[:3]}"
        )

        # The open question, in one assertion. A failure here is a finding, not a
        # flake: it means iOS keeps a Live Photo's .MOV half or an .AAE sidecar
        # after deleting the item they belong to, and the answer belongs in the
        # README either way. Write down exactly which files are listed.
        assert not stats.leftover, (
            "the item was deleted but these files are still on the iPhone: "
            f"{stats.leftover}. This is the behaviour MANUAL_TESTING.md test 17 exists "
            "to discover -- record it before changing anything."
        )

        # The manifest agrees with the phone, so nothing is offered for deletion twice.
        still_live = {row.device_path for row in database.live_records(hw_config.udid)}
        assert not (doomed & still_live)
    finally:
        database.close()
