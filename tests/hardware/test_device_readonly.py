"""What a real iPhone actually answers -- checked, and nothing written back.

These are the parts of ``MANUAL_TESTING.md`` that need a cable but not a pair of
eyes: the handshake, the device panel, the scan, the item arithmetic and the byte
fidelity of a download. Between them they replace the clicking in checks 3, 4, 7
and 8; what they cannot replace is anyone *looking* at a photo, which is why 8
still asks you to open one in Preview.

Nothing here writes to the phone. The AFC channel has no method that could, and
``conftest.py`` disarms the one call that can delete before any of this runs.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ipm.core.library import count_items, item_key
from ipm.device.afc import MEDIA_ROOTS, AfcDeviceBackend
from ipm.device.monitor import list_usb_serials
from ipm.models import RemoteFile
from tests.hardware.conftest import HardwareConfig

pytestmark = pytest.mark.hardware

PART_EXTENSIONS = {".MOV", ".AAE"}
"""Extensions that are part of an item rather than an item of their own.

Measured against a real iPhone 13 (16 984 files, 16 740 groups): the only shapes a
group ever took were a lone still, still + ``.MOV``, ``.AAE`` + still, ``.AAE`` +
``.MOV``, or a lone ``.MOV``. Not one group held two stills.
"""


# ---------------------------------------------------------------------------
# 3. Live detection
# ---------------------------------------------------------------------------


async def test_the_phone_that_answers_is_the_one_we_were_told_about(
    hw_config: HardwareConfig, backend: AfcDeviceBackend
) -> None:
    serials = await list_usb_serials()
    assert hw_config.udid in serials
    info = await backend.get_info()
    assert info.udid == hw_config.udid


async def test_the_device_panel_has_everything_it_promises(
    backend: AfcDeviceBackend,
) -> None:
    """Every field the panel shows is guarded individually in afc.py -- so check
    that the guards are not quietly swallowing all of them."""
    info = await backend.get_info()

    assert info.name
    assert info.ios_version
    # The panel shows a marketing name, not iPhone14,5. A raw product type here
    # means product_types.py has not caught up with a newer model.
    assert info.model_name and "," not in info.model_name
    assert info.storage_total and info.storage_total > 0
    assert info.storage_free is not None
    assert 0 <= info.storage_free <= info.storage_total
    if info.battery_percent is not None:
        assert 0 <= info.battery_percent <= 100


# ---------------------------------------------------------------------------
# 4. The numbers are the right numbers
# ---------------------------------------------------------------------------


async def test_the_scan_never_leaves_the_media_roots(media: list[RemoteFile]) -> None:
    """The scan is deliberately narrow: /PhotoData at large is thumbnails and
    derivatives, none of which are photos the user owns."""
    assert media, "the phone reported no media at all -- is it unlocked and trusted?"
    for entry in media:
        assert entry.path.startswith(MEDIA_ROOTS), entry.path


async def test_every_scanned_file_has_a_usable_size_and_date(
    media: list[RemoteFile],
) -> None:
    """A zero size or a missing date would put a file in the wrong folder, or make
    the delete gate compare against nothing."""
    tomorrow = datetime.now() + timedelta(days=1)
    for entry in media:
        assert entry.size > 0, entry.path
        assert isinstance(entry.created, datetime), entry.path
        assert isinstance(entry.modified, datetime), entry.path
        assert entry.created < tomorrow, f"{entry.path} claims to be from the future"


async def test_a_group_never_holds_two_photos(media: list[RemoteFile]) -> None:
    """The rule the whole delete scope rests on: a group is one photo plus its parts.

    Grouping by ``(folder, base name)`` is only safe to delete by if a group can
    never contain *two* things a user would call separate photos. The parts -- the
    Live Photo ``.MOV`` half, the ``.AAE`` sidecar -- have no library item of their
    own and are meant to go with the still they belong to. A group holding two
    stills would mean deleting one item took an unrelated photo with it.

    A group with *no* still is fine and does occur: a sidecar or a half whose photo
    has already been removed from the phone.
    """
    counts = count_items(media)
    assert counts.files == len(media)
    assert 0 < counts.items <= counts.files
    assert counts.extra_files == counts.files - counts.items

    groups: dict[str, list[RemoteFile]] = {}
    for entry in media:
        groups.setdefault(item_key(entry.path), []).append(entry)

    crowded = {
        key: sorted(entry.path for entry in members)
        for key, members in groups.items()
        if len([e for e in members if Path(e.path).suffix.upper() not in PART_EXTENSIONS]) > 1
    }
    assert not crowded, (
        f"{len(crowded)} group(s) hold more than one photo, so deleting one item would "
        f"take another photo with it: {list(crowded.values())[:5]}"
    )


async def test_the_edits_found_belong_to_photos_in_the_scan(
    backend: AfcDeviceBackend, media: list[RemoteFile]
) -> None:
    """The walk of ``PhotoData/Mutations`` reads completely and names real photos.

    An edit's folder is named after the original, so its key must be the key of an
    item the scan found. If the phone holds edits and none of them lands on a
    scanned item, the mapping is wrong -- and the delete dialog would offer edited
    photos as safe to remove.
    """
    edited = await backend.list_edited_items()
    keys = {item_key(item.path) for item in media}
    renders = {key for key in edited if key.rsplit("/", 1)[-1] == "Adjustments"}
    if not renders:
        pytest.skip("no photo on this phone has an edit")
    owners = {key.rsplit("/", 1)[0] for key in renders}
    assert owners & keys, f"{len(owners)} edited photo(s), none of them in the scan"


async def test_scanning_twice_gives_the_same_answer(
    backend: AfcDeviceBackend, media: list[RemoteFile]
) -> None:
    """The traversal is hand-rolled; a listing that shifts between runs would make
    every later comparison meaningless."""
    again = await backend.list_media()
    first = {entry.path: entry.size for entry in media}
    second = {entry.path: entry.size for entry in again}

    vanished = sorted(set(first) - set(second))
    assert not vanished, f"files disappeared between two scans: {vanished[:5]}"
    for path, size in first.items():
        assert second[path] == size, f"{path} changed size between two scans"


# ---------------------------------------------------------------------------
# 7 and 8. The transfer, and what lands on disk
# ---------------------------------------------------------------------------


async def test_a_download_is_byte_for_byte_repeatable(
    backend: AfcDeviceBackend, media: list[RemoteFile], tmp_path: Path
) -> None:
    """The promise of the whole tool, on one small file.

    Downloaded twice, hashed twice, and compared with the size the phone reports
    and the bytes the callback saw -- which is where the manifest's digest comes
    from, so an inconsistency here would poison the delete gate.
    """
    smallest = min(media, key=lambda entry: entry.size)

    digests: list[str] = []
    for attempt in range(2):
        target = tmp_path / f"copy-{attempt}"
        streamed = hashlib.sha256()
        written = await backend.download_file(
            smallest.path, target, on_chunk=streamed.update
        )
        assert written == smallest.size
        assert target.stat().st_size == smallest.size
        on_disk = hashlib.sha256(target.read_bytes()).hexdigest()
        # The digest taken while streaming must describe the file that was written.
        assert streamed.hexdigest() == on_disk
        digests.append(on_disk)

    assert digests[0] == digests[1], f"{smallest.path} downloaded differently twice"


async def test_the_reported_size_matches_the_stat(
    backend: AfcDeviceBackend, media: list[RemoteFile]
) -> None:
    """``file_size`` is what the importer uses to size the job; the listing is what
    the delete gate compares against. They must be the same number."""
    for entry in sorted(media, key=lambda item: item.size)[:20]:
        assert await backend.file_size(entry.path) == entry.size


# ---------------------------------------------------------------------------
# The brake itself
# ---------------------------------------------------------------------------


async def test_this_suite_cannot_delete_anything(
    hw_config: HardwareConfig, backend: AfcDeviceBackend
) -> None:
    """The guard in conftest.py is load-bearing, so it gets its own test.

    It takes the ``backend`` fixture although it does not use it, so that it skips
    with the rest when no phone is attached. Without that it was the one test in
    this package that could pass on an empty desk, which made a run where nothing
    reached the phone look like a run that had.
    """
    del backend
    if hw_config.may_delete:
        pytest.skip("every delete gate is open on purpose: the brake is off")

    from ipm.device.ptp import PtpService

    with pytest.raises(AssertionError, match="tried to remove photos"):
        await PtpService().delete_assets(["anything"])
