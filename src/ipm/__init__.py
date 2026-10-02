"""iphone-photo-manager: import iPhone media over USB, delete only what is verified.

The package is split into three layers that must not leak into each other:

* :mod:`ipm.device` -- everything that talks to the iPhone (usbmux, lockdown, AFC).
* :mod:`ipm.core` -- pure business logic (database, path organisation, import and
  delete orchestration). Depends only on the abstract device protocol, so it is
  fully unit-testable with a fake backend.
* :mod:`ipm.tui` -- the Textual application (presentation only).
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "1.0.1"
