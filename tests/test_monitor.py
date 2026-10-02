"""Live USB detection logic (no usbmuxd involved: the listing call is patched)."""

from __future__ import annotations

import pytest

from ipm.device import monitor as monitor_module
from ipm.device.monitor import DeviceMonitor


@pytest.fixture
def serials(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Queue of results returned by successive ``list_usb_serials`` calls."""
    queue: list[list[str]] = []

    async def fake_list() -> list[str]:
        return queue.pop(0) if queue else []

    monkeypatch.setattr(monitor_module, "list_usb_serials", fake_list)
    return queue


async def test_first_poll_reports_a_connection(serials: list[list[str]]) -> None:
    serials.append(["ABC"])
    events = await DeviceMonitor().poll()
    assert [(event.kind, event.serial) for event in events] == [("connected", "ABC")]


async def test_steady_state_reports_nothing(serials: list[list[str]]) -> None:
    monitor = DeviceMonitor()
    serials.extend([["ABC"], ["ABC"], ["ABC"]])
    assert len(await monitor.poll()) == 1
    assert await monitor.poll() == []
    assert await monitor.poll() == []


async def test_unplug_then_replug(serials: list[list[str]]) -> None:
    monitor = DeviceMonitor()
    serials.extend([["ABC"], [], ["ABC"]])
    await monitor.poll()
    assert [event.kind for event in await monitor.poll()] == ["disconnected"]
    assert [event.kind for event in await monitor.poll()] == ["connected"]


async def test_swapping_devices_reports_both_changes(serials: list[list[str]]) -> None:
    monitor = DeviceMonitor()
    serials.extend([["ABC"], ["XYZ"]])
    await monitor.poll()
    events = await monitor.poll()
    assert [(event.kind, event.serial) for event in events] == [
        ("disconnected", "ABC"),
        ("connected", "XYZ"),
    ]


async def test_reset_re_announces(serials: list[list[str]]) -> None:
    monitor = DeviceMonitor()
    serials.extend([["ABC"], ["ABC"]])
    await monitor.poll()
    monitor.reset()
    assert [event.kind for event in await monitor.poll()] == ["connected"]


async def test_listing_failures_are_reported_as_no_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Boom(Exception):
        pass

    async def exploding_list_devices() -> list[object]:
        raise Boom("usbmuxd is not running")

    import pymobiledevice3.usbmux as usbmux

    monkeypatch.setattr(usbmux, "list_devices", exploding_list_devices)
    assert await monitor_module.list_usb_serials() == []
