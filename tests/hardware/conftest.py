"""Gates and fixtures for the tests that talk to a real iPhone.

Everything in this package is excluded from a normal ``pytest`` run (the marker is
deselected in ``pyproject.toml``), needs an explicit ``-m hardware``, and refuses to
start unless it has been told, by UDID, which phone it is allowed to touch.

The layers, from cheapest to last-resort:

1. **the marker** -- a distracted ``pytest`` never reaches this code at all;
2. **the read whitelist** -- ``IPM_HW_UDID`` names the one device the suite may
   use at all. A phone that is plugged in but not named is not touched, and a
   mismatch fails loudly rather than skipping quietly, because a silent skip is
   how you end up believing you tested something;
3. **the delete whitelist** -- ``IPM_HW_DESTRUCTIVE_UDID`` must name the same
   phone *again*, in a different setting. Being allowed to read from a phone is
   not the same as being allowed to delete from it, and saying which phone may
   lose photos should take a second, deliberate act rather than a flag;
4. **the blocklist** -- ``IPM_HW_PROTECTED_UDIDS`` lists phones that may never be
   deleted from, whatever else is configured. It overrides both whitelists and
   ``--allow-delete``. Use it for phones that are not yours, and for your own once
   there is a spare to test on;
5. **the deletion guard** -- :func:`no_deletion_is_possible` replaces
   :meth:`PtpService.delete_assets` with a function that raises. Unless every gate
   above is open, no test in this package can delete anything even if it tries,
   and a test that tries fails.

Configuration comes from ``.ipm-hw.env`` in the repository root (git-ignored;
copy ``.ipm-hw.env.example``) or from the environment, which wins.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ipm.device.afc import AfcDeviceBackend
from ipm.device.monitor import list_usb_serials
from ipm.device.ptp import PtpService
from ipm.models import DeviceAsset, RemoteFile

ENV_FILE = Path(__file__).resolve().parents[2] / ".ipm-hw.env"

_CACHE: dict[str, Any] = {}
"""Plain data read once from the phone and reused.

Only loop-independent values live here -- lists of dataclasses, never an open
connection, because each test gets its own event loop and a socket bound to a
closed loop is a confusing way to fail.
"""


@dataclass(frozen=True)
class HardwareConfig:
    """What the suite is allowed to do, and to which phone."""

    udid: str
    destructive_udid: str
    destination: Path | None
    protected: frozenset[str]
    allow_delete: bool

    @property
    def is_protected(self) -> bool:
        """True when the configured phone is one that must never be deleted from."""
        return self.udid in self.protected

    @property
    def may_delete(self) -> bool:
        """Every gate open: the flag, both whitelists agreeing, and no blocklist entry.

        The two whitelists have to name the same phone. Reading from a device and
        removing photos from it are different permissions, and the second one is
        worth spelling out twice rather than inferring from the first.
        """
        return (
            self.allow_delete
            and not self.is_protected
            and bool(self.destructive_udid)
            and self.destructive_udid == self.udid
        )

    def require_destination(self) -> Path:
        """The destination folder, or a failure saying exactly what is wrong with it.

        Checked here rather than when the configuration is read: most of this suite
        does not need a destination at all, and a typo in one setting should not
        fail the tests that never look at it. But when a test *does* need it, a bad
        path used to surface as "no manifest at ...", which reads like "you have not
        imported yet" rather than "that folder does not exist".
        """
        if self.destination is None:
            pytest.skip("IPM_HW_DESTINATION is not set: there is no import history to work from")
        if not self.destination.is_dir():
            pytest.fail(
                f"IPM_HW_DESTINATION points at {self.destination}, which is not a folder. "
                "Check the path in .ipm-hw.env -- a leading ~ is expanded, but only if "
                "it is there."
            )
        return self.destination

    def refusal(self) -> str:
        """Why deletion is not allowed right now, in one sentence, or ``""``."""
        if not self.allow_delete:
            return "--allow-delete was not given"
        if self.is_protected:
            return f"the iPhone {self.udid} is on IPM_HW_PROTECTED_UDIDS"
        if not self.destructive_udid:
            return (
                "IPM_HW_DESTRUCTIVE_UDID is not set: naming the phone that may lose "
                "photos is a separate, deliberate setting"
            )
        if self.destructive_udid != self.udid:
            return (
                f"IPM_HW_DESTRUCTIVE_UDID names {self.destructive_udid}, but the suite "
                f"is configured for {self.udid}"
            )
        return ""


def _read_env_file(path: Path) -> dict[str, str]:
    """Parse a ``KEY=VALUE`` file, ignoring blanks and ``#`` comments."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _setting(name: str, file_values: dict[str, str]) -> str:
    """Return a setting, preferring the environment over the file."""
    return os.environ.get(name) or file_values.get(name, "")


@pytest.fixture(scope="session")
def hw_config(request: pytest.FixtureRequest) -> HardwareConfig:
    """The suite's configuration, or a skip explaining exactly what is missing."""
    values = _read_env_file(ENV_FILE)
    udid = _setting("IPM_HW_UDID", values)
    if not udid:
        pytest.skip(
            "IPM_HW_UDID is not set: the hardware suite refuses to guess which iPhone "
            f"it may use. Copy .ipm-hw.env.example to {ENV_FILE.name} and fill it in."
        )
    destination = _setting("IPM_HW_DESTINATION", values)
    protected = {
        entry.strip()
        for entry in _setting("IPM_HW_PROTECTED_UDIDS", values).split(",")
        if entry.strip()
    }
    return HardwareConfig(
        udid=udid,
        destructive_udid=_setting("IPM_HW_DESTRUCTIVE_UDID", values),
        destination=Path(destination).expanduser() if destination else None,
        protected=frozenset(protected),
        allow_delete=bool(request.config.getoption("--allow-delete")),
    )


@pytest.fixture(autouse=True)
def no_deletion_is_possible(
    hw_config: HardwareConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Make deletion impossible for the whole read-only suite.

    This is not a mock standing in for a phone -- it is a brake. Every other
    fixture here hands out the real device, so the one thing that must not be
    reachable by accident is the one call that removes photos.
    """
    if hw_config.may_delete:
        return

    reason = hw_config.refusal()

    async def refuse(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"a test tried to remove photos from the iPhone, but {reason}")

    monkeypatch.setattr(PtpService, "delete_assets", refuse)


@pytest.fixture
async def backend(hw_config: HardwareConfig) -> Any:
    """A connected, read-only AFC session with the configured phone.

    Reconnected per test on purpose: the handshake costs about a second, and it is
    cheaper than reasoning about a socket shared across event loops.
    """
    serials = await list_usb_serials()
    if not serials:
        pytest.skip("no iPhone is plugged in (usbmux reports no device)")
    if hw_config.udid not in serials:
        pytest.fail(
            f"the iPhone that answered is {serials}, not the configured "
            f"{hw_config.udid}. Refusing to run against a phone this suite was not "
            "told about."
        )
    session = await AfcDeviceBackend.connect(serial=hw_config.udid)
    try:
        yield session
    finally:
        await session.aclose()


@pytest.fixture
async def media(backend: AfcDeviceBackend) -> list[RemoteFile]:
    """The full device listing, scanned once for the whole session."""
    if "media" not in _CACHE:
        _CACHE["media"] = await backend.list_media()
    listing: list[RemoteFile] = _CACHE["media"]
    return listing


@pytest.fixture
async def assets(hw_config: HardwareConfig) -> list[DeviceAsset]:
    """The phone's own library items, catalogued once for the whole session.

    Needs the phone unlocked; a locked phone is a skip with the reason the user
    would see in the application, not a failure.
    """
    # A failure is remembered too: opening the library costs a twenty-second
    # timeout when the phone is locked or absent, and paying it once per test
    # would make a skipped run slower than a real one.
    if "assets_error" in _CACHE:
        pytest.skip(f"the phone's photo library did not open: {_CACHE['assets_error']}")
    if "assets" not in _CACHE:
        service = PtpService()
        try:
            await service.connect()
            _CACHE["assets"] = await service.list_assets()
        except Exception as exc:  # noqa: BLE001 - reported as a skip, not swallowed
            _CACHE["assets_error"] = str(exc)
            pytest.skip(f"the phone's photo library did not open: {exc}")
        finally:
            await service.aclose()
    catalogue: list[DeviceAsset] = _CACHE["assets"]
    return catalogue


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Run the photo-library tests before the ones that scan the file system.

    Opening the library needs the phone unlocked, and scanning a 17 000-file library
    takes twenty seconds a time. With the default collection order the scans went
    first, so on a phone with a thirty-second auto-lock the library tests reached a
    locked device and skipped -- on a phone the tester had unlocked a minute earlier.
    Doing the thing with the deadline first costs nothing and removes the trap.
    """
    library_first: list[pytest.Item] = []
    rest: list[pytest.Item] = []
    for item in items:
        target = library_first if "assets" in getattr(item, "fixturenames", ()) else rest
        target.append(item)
    if library_first:
        items[:] = library_first + rest
