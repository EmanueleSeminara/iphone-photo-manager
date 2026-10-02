"""Deletion of device content that is provably safe to delete.

Deletion is a separate, explicit, user-triggered action. It never follows an import
automatically, and there is deliberately no flag to make it do so.

A file may be removed only when three things hold, all re-checked at click time and
again against a fresh scan taken immediately before anything is deleted:

* the manifest has a row for it;
* the **local copy** still exists with the recorded size (and, when deep
  verification is on, the recorded digest);
* the **file on the phone** is still the one that row was written for -- same size,
  same timestamp -- so a path that has been reused by a new photo can never be
  deleted on the strength of an old copy;
* the **item has no edit on the phone**. iOS keeps an edited photo's render apart
  from the original, this application imports only the original, and deleting the
  item would take the edit with it. Such items stay on the phone until edits are
  imported too.

Anything failing one of those lands in the matching list on
:class:`~ipm.models.DeleteCandidates` and is left on the phone.

The user may also delete only part of what is deletable (the *N* most recent items).
The subset is chosen over whole *items* -- never over loose files -- so a Live
Photo's two halves and its ``.AAE`` sidecar are always removed together or not at all.

**How the removal happens.** Not by unlinking files. That was tried, and it left the
Photos app showing an item whose file no longer existed, which no restart could
clear. The selection is instead matched to the phone's own library items
(:mod:`ipm.core.assets`) and iOS is asked to delete those, exactly as Image Capture
does. What was really removed is then established by **re-scanning the file system**
-- the acknowledgement from the phone is not taken as proof, the absence of the file
is.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ipm.core.assets import AssetPlan, AssetTarget, NearMiss, match_assets
from ipm.core.database import ManifestDatabase, local_copy_is_intact
from ipm.core.digest import digest_of
from ipm.core.formatting import human_bytes
from ipm.core.items import ItemGroup, capture_times, group_by_item, select_newest_items
from ipm.core.library import item_key
from ipm.device.base import AssetService, DeviceBackend
from ipm.errors import IpmError
from ipm.models import (
    DeepVerification,
    DeleteCandidates,
    DeleteStats,
    ImportRecord,
    RemoteFile,
)

__all__ = [
    "DeleteProgress",
    "Deleter",
    "ItemGroup",
    "ProgressCallback",
    "VerifyCallback",
    "VerifyProgress",
    "collect_candidates",
    "group_by_item",
    "select_newest_items",
    "verify_deeply",
]

logger = logging.getLogger(__name__)

_PROGRESS_INTERVAL = 0.1


@dataclass(frozen=True, slots=True)
class DeleteProgress:
    """Snapshot handed to the UI while items are being removed from the device.

    The unit is the *item*, because that is what is actually being deleted; the byte
    figure is what the manifest says those items occupy, and only becomes a fact once
    the confirming scan has run.
    """

    items_done: int = 0
    items_total: int = 0
    bytes_freed: int = 0
    current: str = ""

    @property
    def fraction(self) -> float:
        """Completion ratio in ``0.0..1.0``."""
        if self.items_total <= 0:
            return 0.0
        return min(1.0, self.items_done / self.items_total)


ProgressCallback = Callable[[DeleteProgress], None]


def collect_candidates(
    database: ManifestDatabase,
    destination: Path,
    device_udid: str,
    device_files: Iterable[RemoteFile] | None = None,
    edited_items: Collection[str] | None = None,
) -> DeleteCandidates:
    """Split the manifest into "safe to delete" and "keep on the phone".

    Only rows not already marked as deleted are considered, and each one has to pass
    two independent checks:

    * the **local copy** is stat-ed right now -- the size stored at import time is
      never trusted on its own, because the user may have moved or edited files in
      the meantime;
    * the **device file** is looked up in the latest scan and must still be the same
      one the row was written for (:meth:`~ipm.models.ImportRecord.describes`). A row
      whose device path now holds different bytes is the dangerous case: without this
      check the manifest would authorise deleting a photo that was never imported.

    A row that passes both is still held back when its **item has an edit on the
    phone** (:attr:`~ipm.models.DeleteCandidates.edited_on_device`): every file of
    that item -- still, Live Photo half, sidecar -- stays, because deleting any of
    them deletes the item and with it the edit, which was never imported.

    :param device_files: The most recent listing. Omitting it skips the second check
        entirely, which is only appropriate in tests.
    :param edited_items: Item keys from
        :meth:`~ipm.device.base.DeviceBackend.list_edited_items`. Omitting it skips
        the edit check, which again is only appropriate in tests.
    """
    candidates = DeleteCandidates()
    on_device = (
        {item.path: item for item in device_files} if device_files is not None else None
    )
    if on_device is not None:
        candidates.taken = capture_times(on_device.values())
    for record in database.live_records(device_udid):
        if on_device is not None:
            remote = on_device.get(record.device_path)
            if remote is None:
                candidates.absent_from_device.append(record)
                continue
            if not record.describes(remote):
                candidates.changed_on_device.append(record)
                continue
        if edited_items is not None and item_key(record.device_path) in edited_items:
            candidates.edited_on_device.append(record)
            continue
        if local_copy_is_intact(record, destination):
            candidates.deletable.append(record)
        else:
            candidates.unverified.append(record)
    return candidates


@dataclass(frozen=True, slots=True)
class VerifyProgress:
    """Snapshot handed to the UI while local copies are being re-read."""

    files_done: int = 0
    files_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    current: str = ""


VerifyCallback = Callable[[VerifyProgress], None]


def _why_it_has_no_item(group: ItemGroup, misses: Sequence[NearMiss]) -> str:
    """Explain a group the photo library has nothing to match, with the evidence.

    "It has no item for it" was true but unhelpful: it could not distinguish a photo
    iOS has never heard of from one whose recorded size no longer agrees with the
    library's. The first is normal for a `.MOV` half or a sidecar; the second means
    the file changed under the manifest, and is worth knowing about before a run of
    seventeen thousand.

    :param group: The item that was kept.
    :param misses: What the library held under each of its file names.
    :return: One sentence, aimed at somebody who is not going to read the source.
    """
    almost = [miss for miss in misses if miss.found]
    if almost:
        miss = almost[0]
        sizes = " or ".join(human_bytes(size) for size in miss.found)
        return (
            f"{group.name}: the iPhone's photo library does hold an item called {miss.name}, "
            f"but of {sizes} rather than the {human_bytes(miss.expected)} that was imported, "
            "so it was kept. Re-import it (press i) and try again."
        )
    return (
        f"{group.name}: the iPhone's photo library has no item for it, so it was kept. "
        "Delete it in the Photos app if you no longer want it."
    )


def verify_deeply(
    records: Iterable[ImportRecord],
    destination: Path,
    *,
    on_progress: VerifyCallback | None = None,
    should_stop: Callable[[], bool] | None = None,
    record_digest: Callable[[ImportRecord, str], None] | None = None,
) -> DeepVerification:
    """Re-read every local copy and compare it with the digest taken at import.

    Size tells you a file is *there*; only the digest tells you it is the *same*.
    This is what catches a local copy quietly damaged after the import -- overwritten
    by another program, truncated and refilled, or corrupted by the disk -- before its
    original is deleted from the phone on its word.

    Runs synchronously and reads the whole library, so callers hand it to a thread.

    :param records: Rows already selected for deletion.
    :param destination: Root the local paths hang from.
    :param on_progress: Called after each file, and during large ones.
    :param should_stop: Polled between chunks; returning True aborts the pass.
    :param record_digest: Called for rows that had no digest, with the one just
        computed, so the manifest can remember it.
    """
    result = DeepVerification()
    targets = list(records)
    total_bytes = sum(record.size for record in targets)
    done_bytes = 0
    done_files = 0

    def watcher(name: str, files_done: int) -> Callable[[int], None]:
        """Build the per-file callback that reports progress and honours cancellation."""

        def on_bytes(size: int) -> None:
            nonlocal done_bytes
            done_bytes += size
            if should_stop is not None and should_stop():
                raise _Stopped
            if on_progress is not None:
                on_progress(
                    VerifyProgress(
                        files_done=files_done,
                        files_total=len(targets),
                        bytes_done=done_bytes,
                        bytes_total=total_bytes,
                        current=name,
                    )
                )

        return on_bytes

    for record in targets:
        if should_stop is not None and should_stop():
            result.cancelled = True
            break
        local = record.absolute_local_path(destination)
        name = record.device_path.rsplit("/", 1)[-1]

        try:
            digest = digest_of(local, watcher(name, done_files))
        except _Stopped:
            result.cancelled = True
            break
        except OSError as exc:
            logger.warning("cannot read %s: %s", local, exc)
            result.unreadable.append(record)
            done_files += 1
            continue

        if record.sha256 is None:
            # Imported before digests existed. The file cannot be proven identical to
            # what the device sent, only self-consistent from here on, so it counts as
            # "size only" -- but the digest is stored, and the next check is real.
            result.unhashed.append(record)
            if record_digest is not None:
                record_digest(record, digest)
        elif digest == record.sha256:
            result.verified.append(record)
        else:
            logger.warning("digest mismatch for %s (%s)", record.device_path, name)
            result.mismatched.append(record)
        done_files += 1

    if on_progress is not None:
        on_progress(
            VerifyProgress(
                files_done=done_files,
                files_total=len(targets),
                bytes_done=done_bytes,
                bytes_total=total_bytes,
            )
        )
    return result


class _Stopped(Exception):
    """Internal signal raised from the hashing callback to abort a verification."""


class Deleter:
    """Removes verified library items from the device, and proves what went."""

    def __init__(
        self,
        backend: DeviceBackend,
        database: ManifestDatabase,
        destination: Path,
        *,
        assets: AssetService | None = None,
        on_progress: ProgressCallback | None = None,
        cancel: asyncio.Event | None = None,
        device_files: Iterable[RemoteFile] | None = None,
        edited_items: Collection[str] | None = None,
    ) -> None:
        """
        :param backend: Read-only device access layer, used for the listings.
        :param database: Manifest for the destination folder.
        :param destination: Root the manifest paths are relative to.
        :param assets: The phone's photo library. Required by :meth:`run`; leaving it
            out gives an object that can only inspect and verify, which is what the
            tests of the read-only paths use.
        :param on_progress: Called (throttled) with a :class:`DeleteProgress`.
        :param cancel: Set to stop between two batches.
        :param device_files: Latest device listing, used to confirm that each file
            about to be removed is still the one that was imported.
        :param edited_items: Item keys of the photos with an edit on the phone, from
            the same scan. Those items are never deleted.
        """
        self.backend = backend
        self.assets = assets
        self.database = database
        self.destination = destination
        self.on_progress = on_progress
        self.cancel = cancel or asyncio.Event()
        self.device_files = list(device_files) if device_files is not None else None
        self._on_device = (
            {item.path: item for item in self.device_files}
            if self.device_files is not None
            else None
        )
        self.edited_items = frozenset(edited_items) if edited_items is not None else None
        self._last_emit = 0.0

    async def candidates(self) -> DeleteCandidates:
        """Re-evaluate what may be deleted, off the event loop."""
        return await asyncio.to_thread(
            collect_candidates,
            self.database,
            self.destination,
            self.backend.serial,
            self.device_files,
            self.edited_items,
        )

    async def verify(
        self,
        records: list[ImportRecord],
        on_progress: VerifyCallback | None = None,
    ) -> DeepVerification:
        """Run :func:`verify_deeply` in a worker thread, cancellable.

        Reading the whole selection back is the slowest thing this application does
        on the Mac -- minutes for a large library -- so it happens off the event loop
        and stops as soon as :attr:`cancel` is set.
        """

        def remember(record: ImportRecord, digest: str) -> None:
            self.database.record_digest(record.device_udid, record.device_path, digest)

        return await asyncio.to_thread(
            verify_deeply,
            records,
            self.destination,
            on_progress=on_progress,
            should_stop=self.cancel.is_set,
            record_digest=remember,
        )

    async def gate(self, records: Iterable[ImportRecord]) -> tuple[list[ImportRecord], list[str]]:
        """Re-check the selection against the disk and the phone, right now.

        This is the last chance to notice that something changed between the
        confirmation dialog and the deletion: a local copy the user moved to the
        Trash, or a device path that a new photo has taken over. Both are cheap to
        check and both would cost a photo if missed. The same goes for an item with
        an edit on the phone, whatever list the rows came from.

        :return: The rows that still pass, and one sentence per row that does not.
        """
        eligible: list[ImportRecord] = []
        problems: list[str] = []
        for record in records:
            name = record.device_path.rsplit("/", 1)[-1]
            if self.edited_items is not None and item_key(record.device_path) in self.edited_items:
                problems.append(
                    f"{name}: it was edited on the iPhone, and this version does not import "
                    "edits yet, so it was kept."
                )
                continue
            if not await asyncio.to_thread(local_copy_is_intact, record, self.destination):
                problems.append(
                    f"{name}: the local copy is missing or incomplete, so it was kept "
                    "on the iPhone."
                )
                continue
            if self._on_device is not None:
                remote = self._on_device.get(record.device_path)
                if remote is None or not record.describes(remote):
                    problems.append(
                        f"{name}: the file on the iPhone is no longer the one that was "
                        "imported, so it was kept. Rescan and import again."
                    )
                    continue
            eligible.append(record)
        return eligible, problems

    async def plan(self, records: Iterable[ImportRecord]) -> AssetPlan:
        """Match the selection to the phone's library items.

        :raises RuntimeError: when the deleter was built without an asset service.
        """
        if self.assets is None:
            raise RuntimeError("Deleter was created without an asset service")
        library = await self.assets.list_assets()
        return await asyncio.to_thread(match_assets, list(records), library)

    async def run(self, records: list[ImportRecord] | None = None) -> DeleteStats:
        """Delete the verified items from the device, then prove what went.

        The sequence is deliberate:

        1. re-check every row against the local copy and the last device scan;
        2. match what survives to the phone's own library items, refusing anything
           that has no item or an ambiguous one;
        3. ask iOS to delete those items;
        4. **re-scan the device** and treat the result as the truth. Only rows whose
           file has actually disappeared are marked deleted in the manifest; rows
           whose file is still there are reported as left behind.

        Step 4 is what makes the run trustworthy. The phone acknowledges a delete
        request before the library has finished acting on it, and a Live Photo's
        ``.MOV`` half has no library item of its own -- it is expected to go with the
        still, but that is the phone's behaviour to demonstrate, not ours to assume.
        """
        if self.assets is None:
            raise RuntimeError("Deleter was created without an asset service")

        targets = records if records is not None else (await self.candidates()).deletable
        stats = DeleteStats()

        eligible, problems = await self.gate(targets)
        stats.errors.extend(problems)
        stats.failed += len(problems)
        if not eligible:
            self._emit(DeleteProgress(), force=True)
            return stats

        plan = await self.plan(eligible)
        for group in plan.unmatched:
            stats.errors.append(_why_it_has_no_item(group, plan.near_misses.get(group.key, ())))
        for group in plan.ambiguous:
            stats.errors.append(
                f"{group.name}: more than one photo on the iPhone matches it, so it was "
                "kept rather than risking the wrong one."
            )
        stats.failed += plan.refused
        stats.items_refused = plan.refused

        total = len(plan.targets)
        if total == 0:
            self._emit(DeleteProgress(), force=True)
            return stats

        self._emit(DeleteProgress(items_total=total), force=True)

        attempted = 0

        def on_requested(count: int) -> None:
            nonlocal attempted
            attempted = min(count, total)
            self._emit(
                DeleteProgress(
                    items_done=attempted,
                    items_total=total,
                    current=plan.targets[attempted - 1].group.name if attempted else "",
                )
            )

        identifiers = [target.asset.identifier for target in plan.targets]
        refused_before_asking = stats.failed
        service = self.assets
        assert service is not None  # noqa: S101 - checked at the top of run()

        phone_objected = False
        """Whether the *phone* answered the request with an error.

        Kept apart from :attr:`DeleteStats.errors`, which by this point also holds
        the rows the gate declined and the groups the plan refused -- our own
        decisions, taken before the request was ever sent, about items that are not
        in it. Reading those as "the phone argued" stopped the retry below from ever
        running on a real library, where a handful of unmatched groups is normal.
        """

        async def ask() -> None:
            nonlocal phone_objected
            try:
                await service.delete_assets(identifiers, on_requested, self.cancel.is_set)
            except asyncio.CancelledError:
                stats.cancelled = True
            except IpmError as exc:
                # Reported rather than raised, so the confirming scan below still
                # runs. A refusal that skipped the scan would leave the manifest
                # describing a deletion that never happened.
                phone_objected = True
                stats.errors.append(str(exc))
            if self.cancel.is_set() or attempted < total:
                stats.cancelled = True

        await ask()
        self._emit(DeleteProgress(items_done=attempted, items_total=total), force=True)
        await self._confirm(plan.targets[:attempted], stats)

        if self._worth_asking_again(stats, attempted, phone_objected=phone_objected):
            logger.info("nothing was removed; asking the phone once more")
            stats.retried = True
            # The first confirmation counted every requested file as failed. The
            # second one recounts from scratch, so wind that back rather than add
            # to it.
            stats.failed = refused_before_asking
            attempted = 0
            await ask()
            self._emit(DeleteProgress(items_done=attempted, items_total=total), force=True)
            await self._confirm(plan.targets[:attempted], stats)

        return stats

    @staticmethod
    def _worth_asking_again(
        stats: DeleteStats, attempted: int, *, phone_objected: bool
    ) -> bool:
        """Whether to send the identical request a second time.

        Measured on an iPhone 13 over two days: a delete request is sometimes
        accepted, acknowledged through ``didCompleteDeleteFilesWithError:`` with no
        error, and then simply not acted on -- the asset row and both files stay put,
        verified by pulling ``Photos.sqlite``. Sending the same request again works;
        that was first found by pressing ``d`` twice by hand.

        Retrying a destructive operation needs a reason each time, so all of these
        have to hold:

        * **not one file disappeared.** Measured on files, not items: a Live Photo
          whose still went and whose ``.MOV`` half stayed leaves ``items_deleted``
          at zero while a file *did* go, and that is a partial success, not a
          request the phone ignored. Re-asking would not answer it;
        * **something was actually requested**, so there is a request to repeat;
        * **the phone said nothing was wrong.** An error or a refusal is information,
          and repeating a request the phone has already argued with is not the
          answer. This asks *the phone*, through ``phone_objected``, and not
          :attr:`DeleteStats.errors`: by this point that list also holds every row
          the gate kept back and every group the plan refused, which are our own
          decisions about items the request never contained. Measured on a real
          16 749-item library the plan refuses about 60 groups as a matter of
          course, so reading the list made the retry unreachable on exactly the
          runs it was written for;
        * **the user did not stop the run.**

        The second attempt is safe by construction: the same identifiers, for files
        the confirming scan has just seen still present, and the scan runs again
        afterwards. It cannot reach a photo the first request could not.
        """
        # `stats.leftover` carries its own weight here: it is only ever filled by a
        # confirming scan that succeeded, so a run whose re-scan failed -- which is
        # the other way an error reaches the list after the request -- leaves it
        # empty and stops here without needing a clause of its own.
        return (
            stats.deleted == 0
            and attempted > 0
            and bool(stats.leftover)
            and not phone_objected
            and not stats.cancelled
            and not stats.retried
        )

    async def _confirm(self, attempted: Sequence[AssetTarget], stats: DeleteStats) -> None:
        """Re-scan the phone and record only what really disappeared.

        Runs even after a cancelled or partly failed request, because the point is to
        find out what the phone actually did -- and because a manifest that claims a
        file is gone while it is still on the device would offer to delete it again
        forever.

        Only the items whose deletion was actually requested are examined: an item
        the run never got to is not a failure, it is simply still there.
        """
        expected = [record for target in attempted for record in target.records]
        expected.sort(key=lambda record: record.device_path)
        if not expected:
            return
        try:
            remaining = {item.path for item in await self.backend.list_media()}
        except Exception as exc:  # noqa: BLE001 - the deletion itself already happened
            logger.warning("could not re-scan after deleting", exc_info=exc)
            stats.errors.append(
                "The iPhone could not be re-scanned after the deletion, so nothing was "
                "recorded. Press rescan and delete again; already deleted photos are simply "
                "skipped."
            )
            return

        gone = [record for record in expected if record.device_path not in remaining]
        left = [record for record in expected if record.device_path in remaining]

        if gone:
            await asyncio.to_thread(
                self.database.mark_deleted,
                self.backend.serial,
                [record.device_path for record in gone],
            )
        stats.deleted = len(gone)
        stats.bytes_freed = sum(record.size for record in gone)
        stats.items_deleted = sum(
            1
            for target in attempted
            if all(record.device_path not in remaining for record in target.records)
        )
        stats.leftover = [record.device_path for record in left]
        whole = [
            target
            for target in attempted
            if all(record.device_path in remaining for record in target.records)
        ]
        stats.items_untouched = len(whole)
        stats.untouched = sorted(record.device_path for target in whole for record in target.records)
        # A file that was asked to go and did not is a failure, even when nothing
        # raised: this is where a Live Photo half iOS declined to remove would show.
        stats.failed += len(left)
        if left:
            logger.info("%d file(s) survived the deletion", len(left))

    def _emit(self, progress: DeleteProgress, *, force: bool = False) -> None:
        """Send a throttled progress snapshot to the UI."""
        if self.on_progress is None:
            return
        now = time.monotonic()
        if not force and now - self._last_emit < _PROGRESS_INTERVAL:
            return
        self._last_emit = now
        self.on_progress(progress)
