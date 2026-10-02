"""Joining imported files to the library items the phone itself knows about.

Deletion goes through the phone's own photo service, which removes an *asset* --
the library row and every file behind it -- rather than unlinking a file and leaving
the row pointing at nothing. That service does not speak in device paths, so before
anything can be deleted the rows selected for deletion have to be matched to the
assets they came from.

The join key is ``(original filename, size)``. Measured against a real 16 431-row
manifest and the 16 381 assets of the same library:

* no key was held by two assets, and none by two manifest rows -- unique both ways;
* 16 238 rows matched exactly one asset, none matched more than one;
* 193 rows matched nothing: 176 ``.MOV`` halves of Live Photos, 12 ``.AAE`` sidecars
  and 5 oddments. The halves and the sidecars are *parts* of an item, not items, so
  they are expected to disappear together with the still they belong to -- which is
  why the unit of deletion here is the :class:`~ipm.core.items.ItemGroup` and not the
  individual row.

Nothing in this module imports the image-capture framework: it works on
:class:`~ipm.models.DeviceAsset` values, so every rule below is unit-tested without a
phone attached.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from ipm.core.items import ItemGroup, group_by_item, sort_newest_first
from ipm.models import DeviceAsset, ImportRecord

__all__ = ["AssetPlan", "AssetTarget", "NearMiss", "match_assets"]


@dataclass(frozen=True, slots=True)
class AssetTarget:
    """One library item to delete, and the manifest rows it accounts for.

    :ivar asset: The item as the phone's photo service sees it.
    :ivar group: The manifest rows that will be gone once it is deleted -- the still
        that matched, plus any Live Photo half or ``.AAE`` sidecar sharing its name.
    """

    asset: DeviceAsset
    group: ItemGroup

    @property
    def records(self) -> tuple[ImportRecord, ...]:
        """Rows expected to disappear from the device with this item."""
        return self.group.records

    @property
    def size(self) -> int:
        """Bytes the whole group occupies on the device."""
        return self.group.size


@dataclass(frozen=True, slots=True)
class NearMiss:
    """What the photo library holds under a name the plan could not match.

    Exists to answer one question the log could not: when the application says a
    photo has no library item, is the library empty of that name, or does it hold
    something *almost* right? The two mean completely different things -- the first
    is a photo iOS does not know about, the second is a photo whose size moved under
    us -- and telling them apart used to need a session with the phone attached.

    :ivar name: File name that was looked up.
    :ivar expected: Size the manifest says that file has.
    :ivar found: Sizes the library holds under that name, if any.
    """

    name: str
    expected: int
    found: tuple[int, ...]


@dataclass(slots=True)
class AssetPlan:
    """What can be deleted through the photo service, and what cannot.

    :ivar targets: Items to delete, newest first.
    :ivar unmatched: Groups with no asset at all. They are left on the phone: with no
        library row to remove, deleting their files would recreate exactly the
        stranded-row problem this whole mechanism exists to avoid.
    :ivar ambiguous: Groups whose name and size matched more than one asset. Never
        observed on real hardware, but deleting the wrong photo is unrecoverable, so
        an ambiguous match is refused rather than guessed.
    :ivar near_misses: For each unmatched group, keyed by its item key, what the
        library did hold under the same file names. Evidence, not a decision -- the
        group is refused either way.
    """

    targets: list[AssetTarget] = field(default_factory=list)
    unmatched: list[ItemGroup] = field(default_factory=list)
    ambiguous: list[ItemGroup] = field(default_factory=list)
    near_misses: dict[str, tuple[NearMiss, ...]] = field(default_factory=dict)

    @property
    def records(self) -> list[ImportRecord]:
        """Every manifest row covered by :attr:`targets`, ordered by device path."""
        rows = [record for target in self.targets for record in target.records]
        rows.sort(key=lambda record: record.device_path)
        return rows

    @property
    def total_bytes(self) -> int:
        """Bytes expected to be freed on the device."""
        return sum(target.size for target in self.targets)

    @property
    def refused(self) -> int:
        """How many groups this plan declines to touch."""
        return len(self.unmatched) + len(self.ambiguous)


def match_assets(
    records: Iterable[ImportRecord], assets: Iterable[DeviceAsset]
) -> AssetPlan:
    """Match manifest rows to the phone's library items.

    Rows are grouped into items first, and a group matches an asset when **any** of
    its rows does. That is what lets a Live Photo be deleted as a unit: only its
    still carries an asset of its own, and the ``.MOV`` half riding along in the same
    group is what the deletion is expected to take with it.

    An asset claimed by two different groups is dropped from both. That cannot happen
    while the key is unique, but if a future iOS made it possible, deleting one item
    would silently take the other with it.

    :param records: Rows already verified as safe to delete.
    :param assets: The library items currently on the device.
    :return: The plan, with everything it refuses to delete kept and explained.
    """
    by_key: dict[tuple[str, int], list[DeviceAsset]] = {}
    by_name: dict[str, set[int]] = {}
    for asset in assets:
        by_key.setdefault(asset.key, []).append(asset)
        by_name.setdefault(asset.original_filename, set()).add(asset.size)

    plan = AssetPlan()
    claims: dict[str, list[AssetTarget]] = {}

    for group in group_by_item(records):
        found: dict[str, DeviceAsset] = {}
        for record in group.records:
            name = record.device_path.rsplit("/", 1)[-1]
            for asset in by_key.get((name, record.size), ()):
                found[asset.identifier] = asset
        if not found:
            plan.unmatched.append(group)
            plan.near_misses[group.key] = tuple(
                NearMiss(
                    name=name,
                    expected=record.size,
                    found=tuple(sorted(by_name.get(name, ()))),
                )
                for record in group.records
                for name in (record.device_path.rsplit("/", 1)[-1],)
            )
        elif len(found) > 1:
            plan.ambiguous.append(group)
        else:
            asset = next(iter(found.values()))
            target = AssetTarget(asset=asset, group=group)
            claims.setdefault(asset.identifier, []).append(target)

    for contenders in claims.values():
        if len(contenders) == 1:
            plan.targets.append(contenders[0])
        else:
            plan.ambiguous.extend(target.group for target in contenders)

    plan.targets.sort(key=lambda target: target.group.key, reverse=True)
    plan.targets.sort(key=lambda target: target.group.captured, reverse=True)
    sort_newest_first(plan.unmatched)
    sort_newest_first(plan.ambiguous)
    return plan
