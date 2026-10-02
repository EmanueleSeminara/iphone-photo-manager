"""Deletion through the phone's own photo library (image capture / PTP).

Why this exists
---------------

AFC can remove a file from ``/DCIM``, and it does. What it cannot do is remove the
*library row* that points at it: AFC is jailed to ``/var/mobile/Media`` and the Photos
database lives outside that jail. The consequence was observed on an iPhone 13 running
iOS 26.5 -- the file was gone, the item stayed in the Photos app with a warning badge,
and no restart reconciled it. The mechanism is simply wrong for the job.

Image Capture deletes properly because it speaks a different protocol, in which iOS
itself removes the asset. ``ImageCaptureCore`` exposes exactly that, and this module
is the application's door to it.

Why it is a subprocess
----------------------

``ImageCaptureCore`` installs its run-loop sources on the **main** run loop of the
process. That was measured rather than guessed: after ``ICDeviceBrowser.start()`` on a
secondary thread, that thread's run loop still has no sources and returns from
``runMode:beforeDate:`` millions of times a second, while the main one waits normally
-- so a background thread receives no callbacks at all, and ``stop()`` on it never
returns. The main thread of this application belongs to Textual.

So the framework runs in a child process (:mod:`ipm.device._ptp_worker`) where it can
own the main thread, and this class talks to it over a pipe. Everything here is plain
asyncio, which is what the TUI wants, and a crash inside the Objective-C bridge cannot
take the user's session down with it.

Session-local names
-------------------

This channel does not show ``DCIM`` paths: items are named like
``202608_a/QSCU9090.JPG`` and those names change between sessions. Every item does
carry its ``originalFilename`` -- the real ``IMG_6480.HEIC`` -- which is what
:mod:`ipm.core.assets` joins against the manifest. The identifiers handed out are
opaque and valid only until :meth:`PtpService.aclose`; nothing stores them.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from time import monotonic
from typing import Any

from ipm.errors import IpmError
from ipm.models import DeviceAsset

__all__ = ["PtpService", "ptp_is_available"]

logger = logging.getLogger(__name__)

_WORKER_MODULE = "ipm.device._ptp_worker"

_STOP_POLL = 0.2
"""How often a running request looks up from waiting: for a stop, and for a corpse."""

_SILENCE_TIMEOUT = 300.0
"""Seconds of total silence from the child before it is declared lost.

Deliberately longer than anything it can legitimately spend without speaking: the
browse costs up to :data:`BROWSE_TIMEOUT` (20 s) and the session up to six attempts
of :data:`SESSION_TIMEOUT` with pauses between them (~195 s), after which the
catalogue reports on every pump tick. Silence past that is not slowness."""

_STREAM_LIMIT = 64 * 1024 * 1024
"""How long a single line from the child may be.

The catalogue comes back as one JSON line, and :class:`asyncio.StreamReader`
refuses a line longer than 64 KiB by default -- ``ValueError: Separator is not
found, and chunk exceed the limit``. Measured against a real iPhone 13: a
16 932-item library is **1.6 MiB**, about 99 bytes an item, so the default is out
by a factor of twenty-five and the read fails on every library worth deleting
from. This cap covers roughly 650 000 items; a library past that would need the
catalogue sent in batches rather than a bigger number here."""

_STARTUP_TIMEOUT = 30.0
"""Seconds to allow for the child to start and import the frameworks."""

_MISSING_SUPPORT = (
    "Deleting from the iPhone needs the macOS image-capture support, which is not "
    "installed. Reinstall the application with 'pip install -e .' and try again."
)


def _worker_command() -> list[str]:
    """The command that starts the child.

    A function rather than a constant so the protocol can be exercised against a stub
    worker in the tests: everything below -- framing, progress, errors, stopping, a
    child that dies -- is testable without a phone, and only what happens *inside* the
    real worker needs hardware.
    """
    return [sys.executable, "-m", _WORKER_MODULE]


def ptp_is_available() -> bool:
    """Return True when the image-capture framework is installed on this machine.

    Uses the import machinery rather than an actual import: the framework is only
    ever loaded in the child process, and dragging the Objective-C bridge into the
    TUI process merely to answer a yes/no question would be a poor trade.
    """
    try:
        return all(
            importlib.util.find_spec(name) is not None
            for name in ("Foundation", "ImageCaptureCore")
        )
    except (ImportError, ValueError):  # pragma: no cover - depends on the install
        return False


class PtpService:
    """A session with the phone's photo library, hosted in a child process.

    Usage mirrors the AFC backend: :meth:`connect`, then read or delete, then
    :meth:`aclose`. Instances are not reusable after closing.
    """

    def __init__(self, device_name: str | None = None) -> None:
        """
        :param device_name: Name of the phone as lockdown reports it, used to pick the
            right one when more than one camera is attached. When it matches nothing,
            a single attached device is used anyway.
        """
        self._device_name = device_name
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._closed = False
        self._stderr: list[str] = []
        self._lock = asyncio.Lock()

    # -- public API ------------------------------------------------------------

    async def connect(self, on_progress: Callable[[int], None] | None = None) -> None:
        """Start the child, open a session and wait for the phone's item catalogue.

        :param on_progress: Called with the running item count while the catalogue is
            being built -- about fifteen seconds for a 16 000-item library, far too
            long to leave the screen silent.
        :raises IpmError: when the support is missing, no camera appears, the phone is
            locked, or the catalogue never completes.
        """
        if not ptp_is_available():
            raise IpmError(_MISSING_SUPPORT)
        await self._spawn()
        await self._request(
            {"cmd": "connect", "device_name": self._device_name}, on_progress
        )

    async def list_assets(
        self, on_progress: Callable[[int], None] | None = None
    ) -> list[DeviceAsset]:
        """Return every library item the phone reports."""
        rows = await self._request({"cmd": "list"}, on_progress)
        assets = [
            DeviceAsset(
                identifier=str(row["identifier"]),
                original_filename=str(row["original_filename"]),
                size=int(row["size"]),
                created=_to_datetime(row.get("created")),
            )
            for row in (rows or [])
        ]
        if on_progress is not None:
            on_progress(len(assets))
        return assets

    async def delete_assets(
        self,
        identifiers: Sequence[str],
        on_progress: Callable[[int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        """Ask iOS to delete the given items, in batches.

        Returns once the child has acknowledged every batch it sent. That is **not**
        proof the files are gone -- the caller establishes that by re-scanning the
        file system, which is the only ground truth available.

        :param identifiers: Handles from :meth:`list_assets`, this session only.
        :param on_progress: Called with the number of items requested so far.
        :param should_stop: Polled while the request runs; when it turns true the
            child is asked to stop, and it does so between batches. A request already
            sent is always waited out -- abandoning it would leave us unsure what iOS
            did with it.
        """
        await self._request(
            {"cmd": "delete", "identifiers": list(identifiers)}, on_progress, should_stop
        )

    async def aclose(self) -> None:
        """Close the session and shut the child down, never raising at the user."""
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is None:
            return
        try:
            await asyncio.wait_for(self._request({"cmd": "close"}), timeout=15.0)
        except (TimeoutError, IpmError, OSError) as exc:
            logger.warning("the photo-library session did not close cleanly: %s", exc)
        finally:
            await self._terminate(process)
            self._process = None

    # -- child process ---------------------------------------------------------

    async def _spawn(self) -> None:
        """Start the worker, or explain why it could not start."""
        try:
            self._process = await asyncio.create_subprocess_exec(
                *_worker_command(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_STREAM_LIMIT,
            )
        except OSError as exc:
            raise IpmError(_MISSING_SUPPORT) from exc
        if self._process.stderr is not None:
            self._drain_stderr(self._process.stderr)

    def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        """Keep the child's stderr flowing and remember the tail for diagnostics.

        Without this the pipe fills and the child blocks; with it, a crash during
        startup has something to show instead of a bare "it stopped".
        """

        async def pump() -> None:
            while True:
                line = await stream.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").rstrip()
                logger.debug("ptp worker: %s", text)
                self._stderr.append(text)
                del self._stderr[:-20]

        task = asyncio.create_task(pump())
        # Held only so the task is not garbage-collected mid-flight.
        self._stderr_task = task

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        """End the child, politely first."""
        if process.returncode is not None:
            return
        if process.stdin is not None and not process.stdin.is_closing():
            process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def _request(
        self,
        payload: dict[str, Any],
        on_progress: Callable[[int], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> Any:
        """Send one request and wait for its result, forwarding progress meanwhile.

        :raises IpmError: when the child reports a failure or dies.
        """
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise IpmError("The connection to the iPhone's photo library was lost.")

        async with self._lock:
            self._next_id += 1
            request_id = self._next_id
            line = json.dumps({"id": request_id, **payload}) + "\n"
            try:
                process.stdin.write(line.encode())
                await process.stdin.drain()
            except (OSError, ConnectionResetError) as exc:
                raise IpmError(self._died()) from exc

            stopped = False
            last_heard = monotonic()
            while True:
                if should_stop is not None and not stopped and should_stop():
                    stopped = True
                    with_stop = json.dumps({"cmd": "stop"}) + "\n"
                    try:
                        process.stdin.write(with_stop.encode())
                        await process.stdin.drain()
                    except OSError:  # pragma: no cover - the read below will report it
                        pass

                try:
                    # Never `timeout=None`. A request without a stop callback used to
                    # wait for ever, so a child that died -- or never started -- left
                    # the caller blocked on a pipe that would never speak again. That
                    # is not a hypothetical: it hung a test run for sixty-eight
                    # minutes with no child process in the table at all.
                    raw = await asyncio.wait_for(
                        process.stdout.readline(), timeout=_STOP_POLL
                    )
                except TimeoutError:
                    if process.returncode is not None:
                        # The surest signal there is: the process is gone. Checked
                        # rather than timed, so a crash is reported in a fifth of a
                        # second instead of after some invented interval.
                        raise IpmError(self._died()) from None
                    if monotonic() - last_heard > _SILENCE_TIMEOUT:
                        raise IpmError(self._silent()) from None
                    continue
                except ValueError as exc:
                    # StreamReader gives up on a line longer than its limit and
                    # leaves the rest in the buffer, so every later read is garbage.
                    # Say what happened rather than let it surface as a lost child.
                    raise IpmError(
                        "The iPhone's photo library sent more than this version can "
                        f"read in one go ({_STREAM_LIMIT // 1024 // 1024} MB). Please "
                        "report this, with the number of items in your library."
                    ) from exc
                if not raw:
                    raise IpmError(self._died())
                last_heard = monotonic()

                try:
                    message = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:  # pragma: no cover - the child only writes JSON
                    continue
                if message.get("id") != request_id:
                    continue
                if message.get("event") == "progress":
                    if on_progress is not None:
                        on_progress(int(message.get("count", 0)))
                    continue
                if "error" in message:
                    raise IpmError(str(message["error"]))
                return message.get("data")

    def _silent(self) -> str:
        """Explain a child that is still running but has stopped answering."""
        detail = "; ".join(self._stderr[-3:]).strip()
        base = (
            "The iPhone's photo library stopped responding. Close Image Capture, Photos "
            "or Finder's iPhone view if any of them is open, unplug the cable, plug it "
            "back in and try again."
        )
        return f"{base} ({detail})" if detail else base

    def _died(self) -> str:
        """Explain an unexpected end of the child, with whatever it managed to say."""
        detail = "; ".join(self._stderr[-3:]).strip()
        base = (
            "The connection to the iPhone's photo library stopped unexpectedly. Unplug the "
            "cable, plug it back in, unlock the phone and try again."
        )
        return f"{base} ({detail})" if detail else base


def _to_datetime(value: Any) -> datetime | None:
    """Convert the child's epoch seconds into a naive local timestamp."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value))
    except (TypeError, ValueError, OSError):  # pragma: no cover - defensive
        return None
