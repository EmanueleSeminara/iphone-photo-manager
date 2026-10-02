"""Everything that talks to a physical iOS device (usbmux, lockdown, AFC).

Nothing in :mod:`ipm.core` imports this package's concrete implementation; it only
depends on the :class:`~ipm.device.base.DeviceBackend` protocol, which the test
suite implements with an in-memory fake.
"""

from __future__ import annotations

from ipm.device.base import DeviceBackend

__all__ = ["DeviceBackend"]
