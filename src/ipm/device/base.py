"""The contract between the business logic and the device.

Keeping these protocols tiny is what makes the import and delete logic testable
without an iPhone: the tests provide fake objects with these methods.

There are two of them because the phone answers on two channels that do very
different things:

* :class:`DeviceBackend` is the file system (AFC). It lists and reads files, and it
  is **entirely read-only** -- there is deliberately no method on it that changes
  anything on the phone, and ``tests/test_readonly.py`` enforces that against the
  real implementation;
* :class:`AssetService` is the photo library itself (image capture / PTP). It is the
  only thing that can remove anything, and it removes *items*, so iOS drops the
  library row along with the files. Deleting through the file system instead leaves
  the row behind, which is exactly the bug this split exists to prevent.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol, runtime_checkable

from ipm.models import DeviceAsset, DeviceInfo, RemoteFile

__all__ = [
    "AssetProgress",
    "AssetService",
    "ChunkCallback",
    "DeviceBackend",
    "ScanCallback",
]

ChunkCallback = Callable[[bytes], None]
"""Called with each chunk as it is written; may raise to abort the transfer.

The chunk itself is handed over, not merely its length, so a caller can checksum
the bytes while they stream past instead of reading the finished file back from
disk. Progress consumers take ``len(chunk)``.
"""

ScanCallback = Callable[[int, int], None]
"""Called during a scan with ``(files_seen, bytes_seen)``."""

AssetProgress = Callable[[int], None]
"""Called with a running count of library items listed or requested."""


@runtime_checkable
class DeviceBackend(Protocol):
    """Minimal view of a connected device required by :mod:`ipm.core`."""

    @property
    def serial(self) -> str:
        """Stable identifier of the device (usbmux serial / UDID)."""
        ...

    async def get_info(self) -> DeviceInfo:
        """Read name, model, iOS version, battery and storage figures."""
        ...

    async def list_media(self, on_progress: ScanCallback | None = None) -> list[RemoteFile]:
        """Enumerate every regular file under ``/DCIM``.

        :param on_progress: Optional callback invoked while walking, so a long scan
            can be shown live.
        :return: All files found, in a stable order.
        """
        ...

    async def list_edited_items(self) -> set[str]:
        """Return the item keys of the photos that have an edit stored on the device.

        A key is the one :func:`~ipm.core.library.item_key` gives the original, e.g.
        ``/DCIM/117APPLE/IMG_7130``. Edits are not imported yet, so these items are
        kept off any deletion: removing the photo from the phone would lose the edit.

        :raises Exception: when the edits cannot be listed completely. A partial
            answer must never pass for "no edits".
        """
        ...

    async def file_size(self, remote_path: str) -> int:
        """Return the size of one device file in bytes, or 0 when unavailable.

        Purely informational (it drives the progress bar of the library check), so
        implementations report 0 rather than raising when the path is missing.
        """
        ...

    async def download_file(
        self,
        remote_path: str,
        destination: Path,
        on_chunk: ChunkCallback | None = None,
    ) -> int:
        """Copy one device file to *destination*, byte for byte.

        The implementation must never transcode, resize or otherwise alter the data.
        It writes exactly to *destination* (the caller passes a temporary path and
        performs the atomic rename itself).

        :param on_chunk: Called with the size of every chunk written; if it raises,
            the transfer aborts and the partial file is the caller's to clean up.
        :return: Total number of bytes written.
        """
        ...

    async def aclose(self) -> None:
        """Release the AFC service and the lockdown session."""
        ...


@runtime_checkable
class AssetService(Protocol):
    """The phone's own photo library: the only channel that may remove anything.

    Its unit is the *item*, not the file. Asking it to delete an item makes iOS
    remove the library row and every file behind it -- the still, the Live Photo's
    video half, the ``.AAE`` sidecar -- which is the whole reason deletion does not
    go through the file system.
    """

    async def connect(self, on_progress: AssetProgress | None = None) -> None:
        """Open the session and wait for the phone's item catalogue.

        :param on_progress: Called with the running item count while it is built.
        """
        ...

    async def list_assets(self, on_progress: AssetProgress | None = None) -> list[DeviceAsset]:
        """Return every library item currently on the phone."""
        ...

    async def delete_assets(
        self,
        identifiers: Sequence[str],
        on_progress: AssetProgress | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        """Ask the phone to delete the given items.

        Returning without raising means the requests were acknowledged, **not** that
        the files are gone: the caller confirms that by re-scanning the file system,
        which is the only ground truth available.

        :param identifiers: Handles from :meth:`list_assets`, valid for this session.
        """
        ...

    async def aclose(self) -> None:
        """Close the session and release the connection."""
        ...
