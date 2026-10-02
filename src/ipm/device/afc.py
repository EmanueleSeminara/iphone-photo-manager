"""Real device backend built on ``pymobiledevice3`` (usbmux + lockdown + AFC).

Facts this module depends on, verified against pymobiledevice3 10.x:

* the whole relevant API is ``asyncio``-based;
* :func:`pymobiledevice3.lockdown.create_using_usbmux` is a coroutine returning a
  ``UsbmuxLockdownClient`` whose ``all_values`` dict is populated during the
  handshake (there is no synchronous ``all_values`` property to call);
* :class:`~pymobiledevice3.services.afc.AfcService` is jailed to
  ``/var/mobile/Media``, so ``/DCIM/100APPLE/IMG_0001.HEIC`` is the correct path;
* ``AfcService.stat`` returns ``st_size`` (int), ``st_mtime`` / ``st_birthtime``
  (``datetime``) and ``st_ifmt`` (``S_IFREG`` / ``S_IFDIR`` / ``S_IFLNK``);
* ``AfcService.fread`` serialises concurrent callers on one service instance, so a
  single AFC connection can feed several parallel download tasks.

.. warning::
   This layer was written without a physical iPhone available. Treat every change
   here as unverified until the user confirms it against real hardware.

.. note::
   **Nothing here writes to the phone.** Every method uses read-only AFC calls
   (``listdir``, ``stat``, ``isdir``, ``fopen`` in ``"r"`` mode, ``fread``,
   ``fclose``, ``get_device_info``), and ``tests/test_readonly.py`` parses this file
   to make sure that stays true.

   Deletion used to live here, as ``delete_file``, and was taken out before the first
   release: removing a file over AFC leaves its row in the Photos library pointing at
   nothing, because
   AFC is jailed to ``/var/mobile/Media`` and the library database is not. Deletion
   now goes through :mod:`ipm.device.ptp`, where iOS removes the asset itself. Do not
   reintroduce a writing call here without reading that module first.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import posixpath
from datetime import datetime
from pathlib import Path
from typing import Any

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import AfcService

from ipm.core.product_types import marketing_name
from ipm.device.base import ChunkCallback, ScanCallback
from ipm.models import DeviceInfo, RemoteFile

__all__ = ["AfcDeviceBackend", "CPL_ROOT", "DCIM_ROOT", "LABEL", "MEDIA_ROOTS", "MUTATIONS_ROOT"]

logger = logging.getLogger(__name__)

LABEL = "iphone-photo-manager"
DCIM_ROOT = "/DCIM"
CPL_ROOT = "/PhotoData/CPLAssets"
"""Assets that belong to the library but live outside ``/DCIM``.

Photos saved from other apps, from shared albums, or left behind by a past iCloud
Photos setup are stored here under a UUID file name. They appear in the Photos app
like any other item, so leaving them out would silently under-import the library --
and, worse, make the "everything is safely on the Mac" claim false. Verified on a
real device: the Photos database listed 9 such assets, and this folder held exactly
those 9 files and nothing else.
"""

MEDIA_ROOTS: tuple[str, ...] = (DCIM_ROOT, CPL_ROOT)
"""Roots scanned by :meth:`AfcDeviceBackend.list_media`, in order.

Deliberately *not* the whole of ``/PhotoData``: that tree also holds
``Mutations/`` (rendered copies of edited photos, ~300 files) and tens of thousands
of thumbnails and internal derivatives, none of which are library items.
"""
MUTATIONS_ROOT = "/PhotoData/Mutations"
"""Where iOS keeps the edited version of a photo, apart from the original.

Editing a photo does not touch its file in ``/DCIM``. The result goes to a folder
named after the original, e.g. ``Mutations/DCIM/117APPLE/IMG_7130/Adjustments/``
holding ``FullSizeRender.heic`` for ``/DCIM/117APPLE/IMG_7130.HEIC``, so the path
alone says exactly which photo has an edit. This application does not import those
renders yet, which is why :meth:`AfcDeviceBackend.list_edited_items` exists: a photo
with an edit must not be deleted from the phone, or the edit is lost.
"""

CHUNK_SIZE = 1024 * 1024
"""Read size per AFC round trip. Below the library's 4 MB cap, small enough for a
progress bar that moves smoothly on large videos."""

_BATTERY_DOMAIN = "com.apple.mobile.battery"
_DISK_DOMAIN = "com.apple.disk_usage"


class AfcDeviceBackend:
    """:class:`~ipm.device.base.DeviceBackend` implementation over AFC."""

    def __init__(self, lockdown: Any, afc: AfcService, serial: str) -> None:
        """Use :meth:`connect` instead of constructing this directly."""
        self._lockdown = lockdown
        self._afc = afc
        self._serial = serial

    @classmethod
    async def connect(cls, serial: str | None = None, *, pair_timeout: float = 10.0) -> AfcDeviceBackend:
        """Open a lockdown session and the AFC service for a USB device.

        :param serial: usbmux serial to target; ``None`` picks the only USB device.
        :param pair_timeout: Seconds to wait for the user to answer the trust prompt.
        :raises Exception: any ``pymobiledevice3`` error; callers map it through
            :func:`ipm.errors.friendly_message`.
        """
        lockdown = await create_using_usbmux(
            serial=serial,
            label=LABEL,
            connection_type="USB",
            pair_timeout=pair_timeout,
        )
        afc = AfcService(lockdown=lockdown)
        try:
            await afc.connect()
        except BaseException:
            with contextlib.suppress(Exception):
                await lockdown.close()
            raise
        resolved = serial or str(getattr(lockdown, "udid", "") or "")
        return cls(lockdown, afc, resolved)

    @property
    def serial(self) -> str:
        """Stable identifier used as the manifest's device key."""
        return self._serial

    # -- information -------------------------------------------------------

    async def get_info(self) -> DeviceInfo:
        """Read the values shown in the device panel.

        Every lookup is individually guarded: recent iOS releases restrict some
        lockdown values, and a missing field must never break the panel.
        """
        values: dict[str, Any] = dict(getattr(self._lockdown, "all_values", {}) or {})
        battery = await self._battery_percent()
        total, free = await self._storage()
        product_type = values.get("ProductType")

        return DeviceInfo(
            serial=self._serial,
            name=values.get("DeviceName"),
            product_type=product_type,
            model_name=marketing_name(product_type),
            ios_version=values.get("ProductVersion"),
            battery_percent=battery,
            storage_total=total,
            storage_free=free,
            udid=values.get("UniqueDeviceID") or (self._serial or None),
        )

    async def _battery_percent(self) -> int | None:
        """Return the charge percentage, or ``None`` when the domain is restricted."""
        try:
            value = await self._lockdown.get_value(
                domain=_BATTERY_DOMAIN, key="BatteryCurrentCapacity"
            )
        except Exception as exc:  # noqa: BLE001 - optional field
            logger.debug("battery unavailable: %s", exc)
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    async def _storage(self) -> tuple[int | None, int | None]:
        """Return ``(total_bytes, free_bytes)``, preferring AFC's own device info."""
        try:
            # get_device_info() carries no return annotation upstream.
            info: dict[str, Any] = await self._afc.get_device_info()  # type: ignore[no-untyped-call]
            return int(info["FSTotalBytes"]), int(info["FSFreeBytes"])
        except Exception as exc:  # noqa: BLE001 - fall back to lockdown
            logger.debug("AFC device info unavailable: %s", exc)

        try:
            usage = await self._lockdown.get_value(domain=_DISK_DOMAIN)
        except Exception as exc:  # noqa: BLE001 - optional field
            logger.debug("disk usage unavailable: %s", exc)
            return None, None
        if not isinstance(usage, dict):
            return None, None
        return _as_int(usage.get("TotalDiskCapacity")), _as_int(usage.get("TotalDataAvailable"))

    # -- listing -----------------------------------------------------------

    async def list_media(self, on_progress: ScanCallback | None = None) -> list[RemoteFile]:
        """Walk every root in :data:`MEDIA_ROOTS` and return the regular files found.

        One ``stat`` per entry is issued (the library's own ``walk`` would stat each
        entry a second time). Symlinks and dot-files are skipped: the former are not
        media, the latter are the system's own bookkeeping. A root that does not
        exist on this device is skipped silently.
        """
        found: list[RemoteFile] = []
        total_bytes = 0

        async def scan(directory: str) -> None:
            nonlocal total_bytes
            try:
                entries = await self._afc.listdir(directory)
            except Exception as exc:  # noqa: BLE001 - an unreadable album is not fatal
                logger.warning("cannot list %s: %s", directory, exc)
                return
            for name in sorted(entries):
                if name in (".", "..", "") or name.startswith("."):
                    continue
                full = posixpath.join(directory, name)
                try:
                    stat = await self._afc.stat(full)
                except Exception as exc:  # noqa: BLE001 - skip vanished entries
                    logger.warning("cannot stat %s: %s", full, exc)
                    continue
                kind = stat.get("st_ifmt")
                if kind == "S_IFDIR":
                    await scan(full)
                    continue
                if kind != "S_IFREG":
                    continue
                size = int(stat.get("st_size", 0))
                found.append(
                    RemoteFile(
                        path=full,
                        size=size,
                        created=_as_datetime(stat.get("st_birthtime"))
                        or _as_datetime(stat.get("st_mtime"))
                        or datetime.now(),
                        modified=_as_datetime(stat.get("st_mtime"))
                        or _as_datetime(stat.get("st_birthtime"))
                        or datetime.now(),
                    )
                )
                total_bytes += size
                if on_progress is not None:
                    on_progress(len(found), total_bytes)

        for root in MEDIA_ROOTS:
            try:
                if not await self._afc.isdir(root):
                    continue
            except Exception as exc:  # noqa: BLE001 - absent root, nothing to scan
                logger.debug("media root %s unavailable: %s", root, exc)
                continue
            await scan(root)

        found.sort(key=lambda item: item.path)
        return found

    async def list_edited_items(self) -> set[str]:
        """Return the item keys of the photos that have an edit stored on the device.

        Walks :data:`MUTATIONS_ROOT` and, for every regular file found below it,
        records each folder on the way down with that prefix removed. A render at
        ``Mutations/DCIM/117APPLE/IMG_7130/Adjustments/FullSizeRender.heic`` therefore
        yields ``/DCIM/117APPLE/IMG_7130``, which is exactly the
        :func:`~ipm.core.library.item_key` of the original. The intermediate folders
        (``/DCIM``, ``/DCIM/117APPLE``) end up in the set as well; no item can have
        such a key, so they are harmless, and keeping every ancestor means a layout
        nobody has measured yet (assets outside ``/DCIM``) is still covered.

        A folder with no file anywhere below it does not count: there is no edit in
        it to lose.

        Unlike :meth:`list_media`, this is strict. The answer decides what may be
        deleted, so a listing that fails half-way must not pass for "no edits":
        any error other than a missing path is raised. A missing root means the
        phone has never had an edit, and a path that vanishes mid-walk has nothing
        left in it -- both are simply nothing to report.

        :return: Item keys, plus the folders above them.
        :raises Exception: when part of the tree cannot be read.
        """
        edited: set[str] = set()
        prefix_length = len(MUTATIONS_ROOT)

        async def walk(directory: str) -> bool:
            """Walk *directory*; return whether any regular file lies below it."""
            try:
                entries = await self._afc.listdir(directory)
            except Exception as exc:
                if _is_not_found(exc):
                    return False
                raise
            holds_a_file = False
            for name in sorted(entries):
                if name in (".", "..", "") or name.startswith("."):
                    continue
                full = posixpath.join(directory, name)
                try:
                    stat = await self._afc.stat(full)
                except Exception as exc:
                    if _is_not_found(exc):
                        continue
                    raise
                kind = stat.get("st_ifmt")
                if kind == "S_IFDIR":
                    if await walk(full):
                        holds_a_file = True
                elif kind == "S_IFREG":
                    holds_a_file = True
            if holds_a_file and len(directory) > prefix_length:
                edited.add(directory[prefix_length:])
            return holds_a_file

        await walk(MUTATIONS_ROOT)
        return edited

    async def file_size(self, remote_path: str) -> int:
        """Return the size of one device file, or 0 when it cannot be stat-ed."""
        try:
            stat = await self._afc.stat(remote_path)
            return int(stat.get("st_size", 0))
        except Exception as exc:  # noqa: BLE001 - purely informational
            logger.debug("cannot stat %s: %s", remote_path, exc)
            return 0

    # -- transfer ----------------------------------------------------------

    async def download_file(
        self,
        remote_path: str,
        destination: Path,
        on_chunk: ChunkCallback | None = None,
    ) -> int:
        """Stream one file from the device to *destination*, byte for byte.

        The library's own ``pull`` is deliberately not used: it writes straight to
        the final path (no atomic rename) and offers no per-chunk callback, both of
        which this application needs.
        """
        stat = await self._afc.stat(remote_path)
        size = int(stat.get("st_size", 0))
        # The mode is spelled out although "r" is the library's default: it is the
        # difference between reading the user's photo and truncating it.
        handle = await self._afc.fopen(remote_path, mode="r")
        written = 0
        try:
            with destination.open("wb") as stream:
                while written < size:
                    to_read = min(CHUNK_SIZE, size - written)
                    chunk = await self._afc.fread(handle, to_read)
                    if not chunk:
                        break
                    await asyncio.to_thread(stream.write, chunk)
                    written += len(chunk)
                    if on_chunk is not None:
                        on_chunk(chunk)
        finally:
            with contextlib.suppress(Exception):
                await self._afc.fclose(handle)
        return written

    async def aclose(self) -> None:
        """Close the AFC service and the lockdown session, ignoring teardown errors."""
        with contextlib.suppress(Exception):
            await self._afc.aclose()
        with contextlib.suppress(Exception):
            await self._lockdown.close()


def _is_not_found(exc: BaseException) -> bool:
    """Whether *exc* says the path does not exist, as opposed to any other failure.

    ``pymobiledevice3`` raises its own ``AfcFileNotFoundError`` for a missing path;
    the test stubs raise the built-in ``FileNotFoundError``. Matched by name as well
    as by type, so this module does not depend on where the library defines it.
    """
    return isinstance(exc, FileNotFoundError) or type(exc).__name__.endswith("FileNotFoundError")


def _as_int(value: Any) -> int | None:
    """Best-effort integer conversion for lockdown values."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_datetime(value: Any) -> datetime | None:
    """Normalise an AFC timestamp (already a ``datetime`` in current versions)."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        # Older releases returned nanoseconds since the epoch.
        seconds = value / 1e9 if value > 1e12 else float(value)
        try:
            return datetime.fromtimestamp(seconds)
        except (OverflowError, OSError, ValueError):
            return None
    return None
