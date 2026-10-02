"""Child process that talks to the phone's photo library over image capture.

Why a whole process
-------------------

``ImageCaptureCore`` delivers its callbacks through a Cocoa run loop, and it installs
its sources on the **main** run loop of the process -- measured, not assumed: after
``ICDeviceBrowser.start()`` on a secondary thread that thread's run loop still has no
sources at all and returns from ``runMode:beforeDate:`` instantly, while the main
one waits normally. A background thread therefore never receives a single callback,
and ``stop()`` on it never returns.

The application's main thread belongs to Textual, and asking a TUI to give it up so a
delete button can work would be the tail wagging the dog. So the framework gets a
process of its own, where it can have the main thread it insists on, and the parent
talks to it over a pipe. Two things come free with that: a crash in the Objective-C
bridge takes down this process instead of the user's session, and the parent needs no
threads at all -- it is plain asyncio, which is what Textual wants.

The protocol
------------

One JSON object per line, in both directions. Parent to child::

    {"id": 1, "cmd": "connect", "device_name": "iPhone di Test"}
    {"id": 2, "cmd": "list"}
    {"id": 3, "cmd": "delete", "identifiers": ["0", "7"]}
    {"cmd": "stop"}          # no id: cooperative cancel, honoured between batches
    {"id": 4, "cmd": "close"}

Child to parent::

    {"event": "progress", "id": 1, "count": 4212}
    {"event": "result", "id": 1, "ok": true, "data": ...}
    {"event": "result", "id": 1, "error": "The iPhone is locked. …"}

Every ``error`` string is already phrased for the end user; the parent shows it as it
stands.

Run it with ``python -m ipm.device._ptp_worker``. It is never imported by the parent.
"""

from __future__ import annotations

import contextlib
import json
import queue
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

BROWSE_TIMEOUT = 20.0
"""Seconds to wait for the browser to report the phone. Normally under a second."""

SESSION_TIMEOUT = 30.0
"""Seconds to wait for one attempt at opening the session."""

SESSION_ATTEMPTS = 6
SESSION_RETRY_PAUSE = 3.0
"""How often, and how many times, to retry a session the phone declines.

Measured against an iPhone 13 on iOS 26.5: the browser reports the device within a
second, but the *first* ``requestOpenSession`` after that is refused with -9943
("Please unlock ...") even when the phone is unlocked, in the foreground, and
visible in Apple's own Image Capture. A retry five seconds later opened the session
and enumerated 16 922 files. The device is announced before iOS has finished
making it available, and there is no separate signal for that.

The same code is what a genuinely locked phone returns, and the two cannot be told
apart, so both are retried. That costs a locked phone about twenty seconds before
it is told to unlock -- which is time the user can spend unlocking it."""

CATALOG_TIMEOUT = 600.0
"""Seconds to wait for the item catalogue: ~15 s for 16 000 items, with a lot of room."""

DELETE_TIMEOUT = 900.0
"""Seconds to wait for one delete batch to be acknowledged."""

PUMP_INTERVAL = 0.05
"""How long one run-loop turn may block before the waiting condition is re-checked."""

DELETE_BATCH = 100
"""Items per delete request.

One request per item would attribute failures most precisely, but it is also one round
trip each and a full migration is tens of thousands of items. Correctness does not
depend on the batch anyway: the parent establishes what really went by re-scanning the
file system afterwards.
"""


class Failure(Exception):
    """An error already phrased for the end user."""


def _emit(payload: dict[str, Any]) -> None:
    """Write one protocol line to the parent."""
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


class Session:
    """The image-capture session, living entirely on this process's main thread."""

    def __init__(self) -> None:
        self.devices: list[Any] = []
        self.session_open = False
        self.session_error: str | None = None
        self.catalog_ready = False
        self.items_seen = 0
        self.delete_done = False
        self.delete_error: str | None = None
        self.device_gone = False
        self.device: Any = None
        self.browser: Any = None
        self.items: dict[str, Any] = {}
        self.stop_requested = False
        self._commands: queue.Queue[dict[str, Any]] = queue.Queue()

    # -- run loop ----------------------------------------------------------

    def pump(
        self,
        predicate: Any,
        timeout: float,
        what: str,
        on_tick: Any = None,
    ) -> None:
        """Run the Cocoa run loop until *predicate* holds, or give up.

        The callbacks that satisfy the predicate are delivered *by* this run loop, so
        not pumping would be a deadlock rather than a wait.
        """
        from Foundation import NSDate, NSDefaultRunLoopMode, NSRunLoop

        run_loop = NSRunLoop.currentRunLoop()
        deadline = time.monotonic() + timeout
        while not predicate():
            if self.device_gone:
                raise Failure("The iPhone was disconnected. Reconnect the cable and try again.")
            if time.monotonic() >= deadline:
                raise Failure(
                    f"The iPhone did not respond while {what}. Unplug the cable, plug it back "
                    "in, unlock the phone and try again."
                )
            run_loop.runMode_beforeDate_(
                NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(PUMP_INTERVAL)
            )
            self.drain()
            if on_tick is not None:
                on_tick()

    def drain(self) -> None:
        """Consume anything that arrived on stdin while we were busy.

        Only ``stop`` is acted on mid-command; everything else waits its turn, because
        the parent never sends a second request before the first has answered.
        """
        while True:
            try:
                message = self._commands.get_nowait()
            except queue.Empty:
                return
            if message.get("cmd") == "stop":
                self.stop_requested = True
            else:
                self._commands.put(message)
                return

    # -- commands ----------------------------------------------------------

    def connect(self, device_name: str | None, report: Any) -> None:
        """Find the phone, open a session, wait for its catalogue of items."""
        import ImageCaptureCore as IC

        delegate = _delegate_class().alloc().init()
        delegate.session = self

        browser = IC.ICDeviceBrowser.alloc().init()
        browser.setDelegate_(delegate)
        browser.setBrowsedDeviceTypeMask_(
            IC.ICDeviceTypeMaskCamera | IC.ICDeviceLocationTypeMaskLocal
        )
        browser.start()
        self.browser = browser
        self._delegate = delegate

        try:
            self.pump(lambda: bool(self.devices), BROWSE_TIMEOUT, "looking for the phone")
        except Failure:
            # A phone that is plugged in but locked does not advertise itself as a
            # camera at all, so this is the usual way "locked" shows up here.
            raise Failure(
                "macOS cannot see the iPhone's photo library. Make sure the phone is "
                "unlocked and connected with a data cable, then try again."
            ) from None
        # Devices are announced one callback at a time; a short settle gives a second
        # phone the chance to appear before the name match has to choose.
        self.settle(0.5)
        self.device = self._pick(device_name)

        self.device.setDelegate_(delegate)
        self._open_session()

        self.pump(
            lambda: self.catalog_ready,
            CATALOG_TIMEOUT,
            "listing its photo library",
            on_tick=lambda: report(self.items_seen),
        )

    def _open_session(self) -> None:
        """Open the photo session, retrying a device that is not ready yet.

        See :data:`SESSION_ATTEMPTS` for why one attempt is not enough.

        :raises Failure: when every attempt was refused.
        """

        def attempt() -> str | None:
            self.session_open = False
            self.session_error = None
            self.device.requestOpenSession()
            self.pump(
                lambda: self.session_open or self.session_error is not None,
                SESSION_TIMEOUT,
                "opening the photo session",
            )
            return None if self.session_open else (self.session_error or "no answer")

        error = retry_session(attempt, self.settle)
        if error is not None:
            raise Failure(_session_failure(error))

    def settle(self, seconds: float) -> None:
        """Pump the run loop for a fixed time, letting pending callbacks arrive."""
        from Foundation import NSDate, NSDefaultRunLoopMode, NSRunLoop

        run_loop = NSRunLoop.currentRunLoop()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            run_loop.runMode_beforeDate_(
                NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(PUMP_INTERVAL)
            )

    def _pick(self, device_name: str | None) -> Any:
        """Choose which attached camera is the phone we are working with."""
        if device_name:
            for device in self.devices:
                name = device.name()
                if name is not None and str(name) == device_name:
                    return device
        return self.devices[0]

    def list_items(self) -> list[dict[str, Any]]:
        """Read the catalogue into plain data the parent can use.

        The identifiers handed out are positions in this catalogue: meaningful only
        until the session closes, which is why nothing stores them.
        """
        if self.device is None:
            raise Failure("Not connected to the iPhone's photo library.")
        self.items.clear()
        result: list[dict[str, Any]] = []
        for index, item in enumerate(self.device.mediaFiles() or []):
            original = item.originalFilename()
            if original is None:
                # Nothing to join it to an imported file with, so it can never be a
                # deletion target; leaving it out is the safe reading.
                continue
            identifier = str(index)
            self.items[identifier] = item
            created = item.creationDate()
            result.append(
                {
                    "identifier": identifier,
                    "original_filename": str(original),
                    "size": int(item.fileSize()),
                    "created": float(created.timeIntervalSince1970()) if created else None,
                }
            )
        return result

    def delete(self, identifiers: list[str], report: Any) -> int:
        """Ask iOS to delete the given items, in batches. Returns how many were asked."""
        if self.device is None:
            raise Failure("Not connected to the iPhone's photo library.")
        if any(key not in self.items for key in identifiers):
            raise Failure(
                "The photo library changed while the deletion was being prepared. Rescan "
                "the iPhone and try again."
            )

        requested = 0
        for start in range(0, len(identifiers), DELETE_BATCH):
            self.drain()
            if self.stop_requested:
                break
            batch = [self.items[key] for key in identifiers[start : start + DELETE_BATCH]]
            self.delete_done = False
            self.delete_error = None
            self.device.requestDeleteFiles_(batch)
            self.pump(
                lambda: self.delete_done, DELETE_TIMEOUT, "deleting the selected photos"
            )
            if self.delete_error is not None:
                # This used to be thrown away, and throwing it away is why two
                # deletions that removed nothing could not be explained: the run said
                # "removed 0, kept 2" and the reason iOS gave went nowhere. Whatever
                # it says, it says more than we can work out from the outside.
                raise Failure(
                    "The iPhone did not delete the selected photo(s). It said: "
                    f"{self.delete_error}"
                )
            requested += len(batch)
            report(requested)
        return requested

    def close(self) -> None:
        """Close the session and stop browsing, ignoring anything already gone."""
        device, browser = self.device, self.browser
        self.device = self.browser = None
        self.items.clear()
        if device is not None and device.hasOpenSession():
            device.requestCloseSession()
            with contextlib.suppress(Failure):
                self.pump(lambda: not self.session_open, 10.0, "closing the session")
        if browser is not None:
            browser.stop()

    # -- stdin -------------------------------------------------------------

    def start_reader(self) -> None:
        """Read protocol lines on a background thread; blocking reads are fine there."""

        def reader() -> None:
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                try:
                    self._commands.put(json.loads(line))
                except ValueError:
                    continue
            self._commands.put({"cmd": "close", "id": None})

        threading.Thread(target=reader, name="ptp-stdin", daemon=True).start()

    def next_command(self) -> dict[str, Any] | None:
        """Pump the run loop while waiting for the parent's next request."""
        from Foundation import NSDate, NSDefaultRunLoopMode, NSRunLoop

        run_loop = NSRunLoop.currentRunLoop()
        while True:
            try:
                message = self._commands.get_nowait()
            except queue.Empty:
                run_loop.runMode_beforeDate_(
                    NSDefaultRunLoopMode, NSDate.dateWithTimeIntervalSinceNow_(PUMP_INTERVAL)
                )
                continue
            if message.get("cmd") == "stop":
                self.stop_requested = True
                continue
            return message



def retry_session(
    attempt: Callable[[], str | None],
    settle: Callable[[float], None],
    attempts: int = SESSION_ATTEMPTS,
    pause: float = SESSION_RETRY_PAUSE,
) -> str | None:
    """Call *attempt* until it succeeds, or give up and return the last error.

    Kept apart from the framework so it can be tested without one: it is the piece
    that decides how long a user waits before being told their phone is locked.

    :param attempt: Returns ``None`` on success, or an error string.
    :param settle: Called with a number of seconds between attempts; in the worker
        this pumps the run loop, which is what lets the callbacks arrive at all.
    :param attempts: Maximum number of tries.
    :param pause: Seconds between tries.
    :return: ``None`` on success, or the error from the final attempt.
    """
    error: str | None = "no attempt was made"
    for number in range(attempts):
        error = attempt()
        if error is None:
            return None
        if number < attempts - 1:
            settle(pause)
    return error


def _session_failure(error: str) -> str:
    """Turn an Objective-C error description into one sentence the user can act on."""
    lowered = error.lower()
    if "-9943" in error or "unlock" in lowered:
        return (
            "The iPhone is locked. Unlock it with your passcode, leave it on the home screen "
            "and try again."
        )
    if "-9934" in error or "in use" in lowered:
        return (
            "Another application is using the iPhone's photo library. Close Image Capture, "
            "Photos or Finder's iPhone view and try again."
        )
    return (
        "The iPhone refused access to its photo library. Unlock it, tap Trust if asked, and "
        f"try again. ({error})"
    )


_DELEGATE: Any = None


def _delegate_class() -> Any:
    """Build the Objective-C delegate class on first use.

    Defined inside a function because subclassing ``NSObject`` at import time would
    make this module unimportable where ``pyobjc`` is absent -- which matters, since
    the test suite imports every module in the package.
    """
    global _DELEGATE
    if _DELEGATE is not None:
        return _DELEGATE

    from Foundation import NSObject

    class _Delegate(NSObject):  # type: ignore[misc]
        """Records browser, device and camera callbacks on the owning session.

        Method names are Objective-C selectors, fixed by the frameworks; the
        underscores are how the bridge spells the colons.
        """

        def deviceBrowser_didAddDevice_moreComing_(self, browser: Any, device: Any, more: bool) -> None:
            self.session.devices.append(device)

        def deviceBrowser_didRemoveDevice_moreGoing_(self, browser: Any, device: Any, more: bool) -> None:
            if device in self.session.devices:
                self.session.devices.remove(device)

        def device_didOpenSessionWithError_(self, device: Any, error: Any) -> None:
            if error is None:
                self.session.session_open = True
            else:
                self.session.session_error = str(error)

        def device_didCloseSessionWithError_(self, device: Any, error: Any) -> None:
            self.session.session_open = False

        def device_didEncounterError_(self, device: Any, error: Any) -> None:
            pass

        def didRemoveDevice_(self, device: Any) -> None:
            self.session.device_gone = True

        def deviceDidBecomeReadyWithCompleteContentCatalog_(self, device: Any) -> None:
            self.session.catalog_ready = True

        def cameraDevice_didAddItems_(self, device: Any, items: Any) -> None:
            self.session.items_seen += len(items)

        def cameraDevice_didRemoveItems_(self, device: Any, items: Any) -> None:
            pass

        def cameraDevice_didCompleteDeleteFilesWithError_(self, device: Any, error: Any) -> None:
            if error is not None:
                self.session.delete_error = str(error)
            self.session.delete_done = True

    _DELEGATE = _Delegate
    return _Delegate


def main() -> int:
    """Serve requests until the parent asks to close or closes the pipe."""
    session = Session()
    session.start_reader()

    while True:
        message = session.next_command()
        if message is None:
            return 0
        request_id = message.get("id")
        command = message.get("cmd")

        def report(count: int, request_id: Any = request_id) -> None:
            _emit({"event": "progress", "id": request_id, "count": count})

        try:
            if command == "connect":
                session.stop_requested = False
                session.connect(message.get("device_name"), report)
                data: Any = None
            elif command == "list":
                data = session.list_items()
            elif command == "delete":
                session.stop_requested = False
                data = session.delete(list(message.get("identifiers", [])), report)
            elif command == "close":
                session.close()
                _emit({"event": "result", "id": request_id, "ok": True, "data": None})
                return 0
            else:
                raise Failure(f"Unknown request {command!r}.")
        except Failure as exc:
            _emit({"event": "result", "id": request_id, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - the parent must never see a traceback
            _emit(
                {
                    "event": "result",
                    "id": request_id,
                    "error": "The iPhone's photo library could not be used "
                    f"({type(exc).__name__}: {exc}).",
                }
            )
        else:
            _emit({"event": "result", "id": request_id, "ok": True, "data": data})


if __name__ == "__main__":
    raise SystemExit(main())
