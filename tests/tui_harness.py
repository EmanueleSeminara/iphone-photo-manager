"""Shared plumbing for the tests that drive the Textual layer.

Three of the fixtures here are safety belts rather than conveniences. A test that
builds :class:`~ipm.tui.app.IpmApp` starts its real device poller, its real photo
service and its real "open the Finder" call, so on the maintainer's own machine --
with a phone plugged in -- an unguarded test run would go looking for hardware and
could delete from it. Import them into every module that instantiates the app::

    from tests.tui_harness import no_finder, no_real_devices, no_real_photo_library  # noqa: F401

They are ``autouse``, so importing them is all it takes.

The helpers below exist because every flow in ``MANUAL_TESTING.md`` starts the same
way: an app, a fake phone, and a wait long enough for Textual's workers to finish.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual.pilot import Pilot
from textual.widgets import RichLog

from ipm.config import Config
from ipm.device import monitor as monitor_module
from ipm.models import DeviceInfo, ImportRecord
from ipm.tui import app as app_module
from ipm.tui.app import IpmApp
from tests.fakes import FakeAssetService, FakeBackend, FakeFile, make_file

__all__ = [
    "ATTACHED",
    "activity_log",
    "attach",
    "config_for",
    "default_files",
    "manifest_files",
    "no_finder",
    "no_real_devices",
    "no_real_photo_library",
    "record_for",
    "settle",
]

ATTACHED: dict[str, FakeBackend] = {}
"""The fake phone the current test attached, so the fake photo library can find it."""


@pytest.fixture(autouse=True)
def no_real_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    """The polling loop must never touch usbmuxd during the tests."""

    async def fake_list() -> list[str]:
        return []

    monkeypatch.setattr(monitor_module, "list_usb_serials", fake_list)


@pytest.fixture(autouse=True)
def no_real_photo_library(monkeypatch: pytest.MonkeyPatch) -> None:
    """The delete flow must never open a real image-capture session.

    Without this the tests would go looking for an actual camera over USB, and on a
    machine with a phone plugged in they would try to delete from it.
    """
    ATTACHED.clear()

    def factory(device_name: str | None = None) -> FakeAssetService:
        backend = ATTACHED.get("backend")
        assert backend is not None, "attach a fake device before deleting"
        return FakeAssetService(backend)

    monkeypatch.setattr(app_module, "PtpService", factory)


@pytest.fixture(autouse=True)
def no_finder(monkeypatch: pytest.MonkeyPatch) -> None:
    """A finished import must not pop a Finder window open during the tests."""
    monkeypatch.setattr(app_module, "open_in_file_manager", lambda path: False)


def config_for(destination: Path, *, warning_seen: bool = True) -> Config:
    """A configuration for a test app.

    The "this does not go to Recently Deleted" warning counts as already dismissed
    by default. It is shown before a user's *first* deletion, and a test about the
    import plan or the delete gate is not about that first time -- only the tests
    that name the warning pass ``warning_seen=False``.

    :param destination: Folder the app should use.
    :param warning_seen: Whether the permanence warning has already been dismissed.
    """
    return Config(destination=destination, permanent_delete_warning_seen=warning_seen)


def default_files() -> tuple[FakeFile, ...]:
    """Two stills a day apart: the smallest library that can still be half-deleted."""
    return (
        make_file("/DCIM/A.HEIC", b"aaa", datetime(2024, 3, 1, 9, 0)),
        make_file("/DCIM/B.HEIC", b"bbbb", datetime(2024, 3, 2, 9, 0)),
    )


async def attach(app: IpmApp, *files: FakeFile) -> FakeBackend:
    """Give a running app a fake iPhone, as :meth:`IpmApp._attach_device` would.

    :param app: The application under test, already mounted.
    :param files: Contents of the fake phone; :func:`default_files` when omitted.
    :return: The backend, so the test can inspect downloads and deletions.
    """
    backend = FakeBackend.with_files(*(files or default_files()))
    app._backend = backend  # type: ignore[assignment]
    app._device = DeviceInfo(serial=backend.serial, name="Test iPhone")
    app._media = await backend.list_media()
    app._edited = await backend.list_edited_items()
    ATTACHED["backend"] = backend
    return backend


async def settle(pilot: Pilot[None], times: int = 12) -> None:
    """Let Textual's workers run to completion.

    Every flow in the app is an ``@work`` coroutine that awaits a modal, so a single
    ``pause()`` is rarely enough and a fixed sleep would be both slower and flakier.

    :param pilot: The pilot from ``app.run_test()``.
    :param times: How many message-pump turns to allow.
    """
    for _ in range(times):
        await pilot.pause()


def activity_log(app: IpmApp) -> str:
    """Return everything written to the activity log, as one plain string."""
    log = app.query_one(RichLog)
    return "\n".join(strip.text for strip in log.lines)


def record_for(path: str, local: str, size: int, mtime: float) -> ImportRecord:
    """Build a manifest row without going through an import."""
    return ImportRecord(
        device_udid="UDID",
        device_path=path,
        size=size,
        mtime=mtime,
        local_path=local,
        imported_at=datetime.now(UTC),
    )


def manifest_files(destination: Path) -> list[Path]:
    """The manifest and its sidecars, for the tests that simulate losing them."""
    return sorted((destination / ".ipm").glob("manifest.db*"))
