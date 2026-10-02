"""In-memory device stand-ins used by every test that would otherwise need an iPhone.

There are two, mirroring the two channels the real phone answers on:

* :class:`FakeBackend` is the file system (AFC). Like the real one, it is read-only:
  no method on it removes anything;
* :class:`FakeAssetService` is the photo library. It is the only thing that deletes,
  and it deletes *items* -- removing a still takes its Live Photo ``.MOV`` half and
  its ``.AAE`` sidecar with it, which is what iOS is expected to do. Set
  :attr:`FakeAssetService.leave_related` to model a phone that does not, so the
  "files left behind" reporting can be tested without hardware.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ipm.core.library import item_key
from ipm.device.base import AssetProgress, ChunkCallback, ScanCallback
from ipm.errors import IpmError
from ipm.models import DeviceAsset, DeviceInfo, RemoteFile

__all__ = ["FakeAssetService", "FakeBackend", "FakeFile", "make_file"]


@dataclass
class FakeFile:
    """A file that "exists" on the fake device."""

    path: str
    content: bytes
    created: datetime
    modified: datetime

    @property
    def remote(self) -> RemoteFile:
        """The metadata view the backend exposes."""
        return RemoteFile(
            path=self.path,
            size=len(self.content),
            created=self.created,
            modified=self.modified,
        )


def make_file(
    path: str,
    content: bytes = b"data",
    created: datetime | None = None,
    modified: datetime | None = None,
) -> FakeFile:
    """Build a :class:`FakeFile` with sensible defaults."""
    stamp = created or datetime(2024, 3, 14, 12, 0, 0)
    return FakeFile(path=path, content=content, created=stamp, modified=modified or stamp)


@dataclass
class FakeBackend:
    """Implements :class:`~ipm.device.base.DeviceBackend` over a dict of files."""

    files: dict[str, FakeFile] = field(default_factory=dict)
    device_serial: str = "FAKE-SERIAL"
    fail_paths: set[str] = field(default_factory=set)
    """Paths whose download raises, to exercise the error paths."""
    downloads: list[str] = field(default_factory=list)
    deletions: list[str] = field(default_factory=list)
    """Paths that disappeared from the device.

    Nothing on this class ever appends to it -- the backend cannot delete. It is
    filled by :class:`FakeAssetService`, because from the file system's point of view
    that is exactly what happens: the photo library removes the files.
    """
    chunk_size: int = 4
    closed: bool = False
    edited: set[str] = field(default_factory=set)
    """Item keys (``/DCIM/100APPLE/IMG_0001``) of photos with an edit on the phone."""
    fail_edits: bool = False
    """Make :meth:`list_edited_items` raise, as a phone that cannot be read would."""

    @classmethod
    def with_files(cls, *files: FakeFile, **kwargs: object) -> FakeBackend:
        """Convenience constructor from a list of :class:`FakeFile`."""
        return cls(files={item.path: item for item in files}, **kwargs)  # type: ignore[arg-type]

    @property
    def serial(self) -> str:
        return self.device_serial

    async def get_info(self) -> DeviceInfo:
        return DeviceInfo(
            serial=self.device_serial,
            name="Fake iPhone",
            product_type="iPhone14,5",
            model_name="iPhone 13",
            ios_version="17.4",
            battery_percent=80,
            storage_total=128 * 1024**3,
            storage_free=64 * 1024**3,
            udid=self.device_serial,
        )

    async def list_media(self, on_progress: ScanCallback | None = None) -> list[RemoteFile]:
        result: list[RemoteFile] = []
        seen_bytes = 0
        for path in sorted(self.files):
            remote = self.files[path].remote
            result.append(remote)
            seen_bytes += remote.size
            if on_progress is not None:
                on_progress(len(result), seen_bytes)
        return result

    async def list_edited_items(self) -> set[str]:
        if self.fail_edits:
            raise OSError("the edits could not be listed")
        return set(self.edited)

    async def file_size(self, remote_path: str) -> int:
        item = self.files.get(remote_path)
        return len(item.content) if item is not None else 0

    async def download_file(
        self,
        remote_path: str,
        destination: Path,
        on_chunk: ChunkCallback | None = None,
    ) -> int:
        if remote_path in self.fail_paths:
            raise OSError("simulated transfer failure")
        item = self.files[remote_path]
        self.downloads.append(remote_path)
        written = 0
        with destination.open("wb") as stream:
            for offset in range(0, len(item.content), self.chunk_size):
                chunk = item.content[offset : offset + self.chunk_size]
                stream.write(chunk)
                written += len(chunk)
                if on_chunk is not None:
                    on_chunk(chunk)
        return written

    async def aclose(self) -> None:
        self.closed = True


_NEVER_AN_ITEM = (".AAE",)
"""Extensions that are always part of an item and never one on their own."""

_SECONDARY = (".MOV", ".AAE")
"""Extensions that lose to a still when both are present under the same base name."""


@dataclass
class FakeAssetService:
    """Implements :class:`~ipm.device.base.AssetService` over a :class:`FakeBackend`.

    The library it exposes is derived from the backend's files the way a real one is:
    one item per ``(folder, base name)`` group, represented by the still. A Live
    Photo's ``.MOV`` half and its ``.AAE`` sidecar get no item of their own, so the
    only way to remove them is to remove the item they belong to.
    """

    backend: FakeBackend
    leave_related: bool = False
    """Model a phone that deletes the still but leaves the half and the sidecar."""
    fail_identifiers: set[str] = field(default_factory=set)
    """Items whose deletion is silently ignored, as a failed request would be."""
    extra_assets: list[DeviceAsset] = field(default_factory=list)
    """Items with no file behind them, e.g. the rendered versions of edited photos."""
    hidden: set[str] = field(default_factory=set)
    """Device paths the library reports no item for, so their group cannot be matched.

    The other half of :attr:`extra_assets`: a file the phone holds but its photo
    library has never heard of. Real and unremarkable -- on a 16 749-item iPhone 13
    the plan refused about 60 groups for exactly this reason -- which is why a run
    containing some has to keep behaving like an ordinary run.
    """
    ignore_first_request: bool = False
    """Accept the first delete request, acknowledge it, and do nothing.

    What a real iPhone 13 did three times over two days: no error, no deletion, and
    the identical request straight afterwards worked.
    """
    refusal: str | None = None
    """When set, ``delete_assets`` raises it -- a phone that answers with an error.

    Observed on a real iPhone: the request is accepted, the callback comes back with
    an error, and nothing is removed. Until 2026-09-05 that error was discarded.
    """
    requested: list[str] = field(default_factory=list)
    """Item identifiers the caller asked to delete, in order."""
    connected: bool = False
    closed: bool = False

    async def connect(self, on_progress: AssetProgress | None = None) -> None:
        self.connected = True
        if on_progress is not None:
            on_progress(len(self.backend.files))

    async def list_assets(self, on_progress: AssetProgress | None = None) -> list[DeviceAsset]:
        assets = [
            DeviceAsset(
                identifier=path,
                original_filename=path.rsplit("/", 1)[-1],
                size=len(self.backend.files[path].content),
                created=self.backend.files[path].created,
            )
            for path in self._primaries()
            if path not in self.hidden
        ]
        assets.extend(self.extra_assets)
        if on_progress is not None:
            on_progress(len(assets))
        return assets

    async def delete_assets(
        self,
        identifiers: Sequence[str],
        on_progress: AssetProgress | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        if self.refusal is not None:
            raise IpmError(self.refusal)
        if self.ignore_first_request:
            self.ignore_first_request = False
            self.requested.extend(identifiers)
            if on_progress is not None:
                on_progress(len(identifiers))
            return
        for done, identifier in enumerate(identifiers, start=1):
            if should_stop is not None and should_stop():
                break
            self.requested.append(identifier)
            if identifier not in self.fail_identifiers:
                self._remove_item(identifier)
            if on_progress is not None:
                on_progress(done)

    async def aclose(self) -> None:
        self.closed = True

    # -- internals ---------------------------------------------------------

    def _primaries(self) -> list[str]:
        """One representative path per item, the way the phone reports items."""
        groups: dict[str, list[str]] = {}
        for path in sorted(self.backend.files):
            groups.setdefault(item_key(path), []).append(path)
        chosen: list[str] = []
        for members in groups.values():
            candidates = [p for p in members if not p.upper().endswith(_NEVER_AN_ITEM)]
            if not candidates:
                continue
            stills = [p for p in candidates if not p.upper().endswith(_SECONDARY)]
            chosen.append(stills[0] if stills else candidates[0])
        return sorted(chosen)

    def _remove_item(self, identifier: str) -> None:
        """Remove the item's file and, unless told otherwise, everything with it."""
        if identifier not in self.backend.files:
            return
        key = item_key(identifier)
        members = (
            [identifier]
            if self.leave_related
            else [path for path in list(self.backend.files) if item_key(path) == key]
        )
        for path in members:
            self.backend.files.pop(path, None)
            self.backend.deletions.append(path)
