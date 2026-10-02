"""Live detection of USB devices, without hammering the phone.

``usbmux.list_devices()`` only asks the local ``usbmuxd`` daemon which devices are
plugged in; it involves no traffic to the phone and no lockdown handshake. That is
why it is safe to call every couple of seconds. The expensive part -- opening a
lockdown session and reading values -- happens exactly once, when a serial appears
in the list that was not there before.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

__all__ = ["DeviceEvent", "DeviceMonitor", "list_usb_serials"]

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 1.5


@dataclass(frozen=True, slots=True)
class DeviceEvent:
    """A device appeared on, or disappeared from, the USB bus."""

    kind: str
    """``"connected"`` or ``"disconnected"``."""
    serial: str


async def list_usb_serials() -> list[str]:
    """Return the serials of every device currently connected over USB.

    Network ("Wi-Fi sync") devices are filtered out: AFC over Wi-Fi is slow and
    unreliable for a full library transfer, and the user asked about the cable.
    Any usbmuxd failure is reported as "no devices" rather than raised, so the
    polling loop can never die.
    """
    try:
        from pymobiledevice3.usbmux import list_devices
    except Exception as exc:  # noqa: BLE001 - library missing or broken install
        logger.warning("usbmux unavailable: %s", exc)
        return []
    try:
        devices = await list_devices()
    except Exception as exc:  # noqa: BLE001 - usbmuxd not running, socket error, ...
        logger.debug("list_devices failed: %s", exc)
        return []
    return [device.serial for device in devices if getattr(device, "is_usb", True)]


class DeviceMonitor:
    """Polls usbmuxd and yields connect/disconnect events."""

    def __init__(self, poll_interval: float = DEFAULT_POLL_INTERVAL) -> None:
        """
        :param poll_interval: Seconds between two usbmuxd queries.
        """
        self.poll_interval = poll_interval
        self._known: set[str] = set()

    def reset(self) -> None:
        """Forget the current state, so the next poll re-announces every device."""
        self._known.clear()

    async def poll(self) -> list[DeviceEvent]:
        """Query usbmuxd once and return the changes since the previous call."""
        current = set(await list_usb_serials())
        events = [
            DeviceEvent("disconnected", serial) for serial in sorted(self._known - current)
        ]
        events += [DeviceEvent("connected", serial) for serial in sorted(current - self._known)]
        self._known = current
        return events

    async def watch(self) -> AsyncIterator[DeviceEvent]:
        """Yield events forever, sleeping :attr:`poll_interval` between polls."""
        while True:
            for event in await self.poll():
                yield event
            await asyncio.sleep(self.poll_interval)
