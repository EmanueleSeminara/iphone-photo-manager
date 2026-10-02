"""The phone's own photo library, read through PTP -- and the delete plan, unarmed.

Two things are checked here that nothing else can check:

* the **join key**. Matching a manifest row to a library item is done on
  ``(original filename, file size)``, and the whole design rests on that pair being
  unique on both sides. It was measured once, by hand, against a 16 000-item
  library; this turns the measurement into something that keeps being true;
* the **plan**, built end to end from the real manifest and the real library, and
  then thrown away. It is the deletion up to the last instruction, which is the
  most that can be automated without removing a photo.

Nothing here deletes. ``conftest.py`` has already replaced
:meth:`PtpService.delete_assets` with a function that raises, and the plan is never
handed to anything that would run it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from ipm.core.assets import match_assets
from ipm.core.database import ManifestDatabase, manifest_path_for
from ipm.core.deleter import Deleter, collect_candidates
from ipm.core.items import group_by_item
from ipm.device.afc import AfcDeviceBackend
from ipm.models import DeviceAsset, RemoteFile
from tests.hardware.conftest import HardwareConfig

pytestmark = pytest.mark.hardware


class CachedLibrary:
    """An :class:`~ipm.device.base.AssetService` over an already-read catalogue.

    Re-cataloguing costs fifteen seconds a time, and the point of these tests is
    the matching, not the enumeration. Deleting raises, loudly: this class exists
    inside a suite that must not remove anything.
    """

    def __init__(self, catalogue: Sequence[DeviceAsset]) -> None:
        self._catalogue = list(catalogue)

    async def connect(self, on_progress: Callable[[int], None] | None = None) -> None:
        return None

    async def list_assets(
        self, on_progress: Callable[[int], None] | None = None
    ) -> list[DeviceAsset]:
        return list(self._catalogue)

    async def delete_assets(
        self,
        identifiers: Sequence[str],
        on_progress: Callable[[int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        raise AssertionError("the read-only hardware suite tried to delete library items")

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------


async def test_the_library_enumerates_and_every_item_is_usable(
    assets: list[DeviceAsset],
) -> None:
    assert assets, "the photo library reported no items at all"
    for asset in assets:
        assert asset.identifier
        assert asset.original_filename
        assert asset.size > 0


async def test_the_join_key_is_unique_across_the_library(
    assets: list[DeviceAsset],
) -> None:
    """``(original filename, size)`` must not be held by two different items.

    If it ever is, ``match_assets`` refuses those groups rather than guessing --
    but a phone where that happens routinely would make deletion useless, so it is
    worth knowing.
    """
    seen: dict[tuple[str, int], list[str]] = {}
    for asset in assets:
        seen.setdefault(asset.key, []).append(asset.identifier)
    clashes = {key: ids for key, ids in seen.items() if len(ids) > 1}
    assert not clashes, f"{len(clashes)} key(s) are held by more than one library item"


async def test_the_library_and_the_file_system_broadly_agree(
    assets: list[DeviceAsset], media: list[RemoteFile]
) -> None:
    """Two independent readings of the same phone: PTP items and AFC files.

    They are not expected to be equal -- Live Photo halves and sidecars have no
    item, and rendered edits have no file under the scanned roots -- but the item
    count must not exceed the file count, and most items must be findable.
    """
    by_key = {(Path(entry.path).name, entry.size) for entry in media}
    matched = sum(1 for asset in assets if asset.key in by_key)
    assert len(assets) <= len(media)
    # The known gap is the rendered edited copies in Mutations/, historically ~1%.
    assert matched >= len(assets) * 0.9, (
        f"only {matched} of {len(assets)} library items were found in the file scan; "
        "the media roots may need revisiting"
    )


# ---------------------------------------------------------------------------
# The plan, built and thrown away
# ---------------------------------------------------------------------------


@pytest.fixture
def manifest(hw_config: HardwareConfig) -> ManifestDatabase:
    """The manifest of the configured destination, opened read-write but only read."""
    path = manifest_path_for(hw_config.require_destination())
    if not path.exists():
        pytest.skip(f"no manifest at {path}: import something into that folder first")
    database = ManifestDatabase(path)
    try:
        yield database
    finally:
        database.close()


async def test_the_delete_gate_only_ever_offers_verified_copies(
    hw_config: HardwareConfig,
    backend: AfcDeviceBackend,
    media: list[RemoteFile],
    manifest: ManifestDatabase,
) -> None:
    """Every row the gate lets through must have a local copy of the right size."""
    assert hw_config.destination is not None
    candidates = await asyncio.to_thread(
        collect_candidates,
        manifest,
        hw_config.destination,
        hw_config.udid,
        media,
    )
    assert candidates.deletable or candidates.held_back, "the manifest has nothing to say"

    for record in candidates.deletable:
        local = hw_config.destination / record.local_path
        assert local.is_file(), f"{record.local_path} was offered without a local copy"
        assert local.stat().st_size == record.size

    on_device = {entry.path: entry.size for entry in media}
    for record in candidates.deletable:
        assert on_device.get(record.device_path) == record.size


async def test_a_full_delete_plan_can_be_built_and_is_not_run(
    hw_config: HardwareConfig,
    backend: AfcDeviceBackend,
    media: list[RemoteFile],
    assets: list[DeviceAsset],
    manifest: ManifestDatabase,
) -> None:
    """The whole deletion, up to the instruction that would remove something.

    This is the dry run: gate, match, inspect. Nothing is handed to
    ``delete_assets``, and the library used here raises if anything tries.
    """
    assert hw_config.destination is not None
    candidates = await asyncio.to_thread(
        collect_candidates,
        manifest,
        hw_config.destination,
        hw_config.udid,
        media,
    )
    if not candidates.deletable:
        pytest.skip("nothing in this folder is currently deletable")

    library = CachedLibrary(assets)
    deleter = Deleter(
        backend,
        manifest,
        hw_config.destination,
        assets=library,
        device_files=media,
    )
    eligible, problems = await deleter.gate(candidates.deletable)
    assert eligible, f"the gate rejected everything: {problems[:3]}"

    plan = await deleter.plan(eligible)

    # Every target is one library item standing for one whole group of files.
    identifiers = [target.asset.identifier for target in plan.targets]
    assert len(identifiers) == len(set(identifiers)), "two groups claim the same item"
    for target in plan.targets:
        for record in target.group.records:
            assert (hw_config.destination / record.local_path).is_file()

    # Matching the same input twice must give the same answer; the matcher is pure.
    again = await asyncio.to_thread(match_assets, list(eligible), list(assets))
    assert [t.asset.identifier for t in again.targets] == identifiers

    covered = len(plan.targets)
    total = len(group_by_item(eligible))
    assert covered > 0, (
        f"none of the {total} deletable item(s) could be matched to a library item; "
        "deletion would refuse everything"
    )
