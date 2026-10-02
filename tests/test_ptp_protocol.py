"""The pipe between the application and the photo-library worker.

The worker itself needs a phone, but everything around it does not: framing, progress
forwarding, an error turned into a sentence, stopping a run in flight, and a child
that dies mid-request. Those are exercised here against a stub worker, because they
are the parts that decide whether a real deletion is reported honestly.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path

import pytest

from ipm.device import ptp as ptp_module
from ipm.device._ptp_worker import (
    SESSION_ATTEMPTS,
    SESSION_RETRY_PAUSE,
    retry_session,
)
from ipm.device.ptp import PtpService
from ipm.errors import IpmError

STUB = textwrap.dedent(
    '''
    """A worker that answers the protocol without ever touching a device."""
    import json, sys

    def emit(payload):
        sys.stdout.write(json.dumps(payload) + "\\n")
        sys.stdout.flush()

    stopped = False
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        command, request_id = message.get("cmd"), message.get("id")
        if command == "stop":
            stopped = True
            continue
        if command == "connect":
            emit({"event": "progress", "id": request_id, "count": 7})
            emit({"event": "result", "id": request_id, "ok": True, "data": None})
        elif command == "list":
            emit({"event": "result", "id": request_id, "ok": True, "data": [
                {"identifier": "0", "original_filename": "IMG_0001.HEIC",
                 "size": 1234, "created": 1700000000.0},
                {"identifier": "1", "original_filename": "IMG_0002.MOV",
                 "size": 99, "created": None},
            ]})
        elif command == "delete":
            emit({"event": "result", "id": request_id,
                  "ok": True, "data": 0 if stopped else len(message["identifiers"])})
        elif command == "explode":
            emit({"event": "result", "id": request_id,
                  "error": "The iPhone is locked. Unlock it and try again."})
        elif command == "vanish":
            sys.exit(3)
        elif command == "close":
            emit({"event": "result", "id": request_id, "ok": True, "data": None})
            break
    '''
)


BIG_STUB = textwrap.dedent(
    '''
    """A worker whose catalogue is bigger than one asyncio line buffer.

    Real libraries are: an iPhone 13 with 16 932 items answers `list` with 1.6 MiB
    on a single line, and asyncio's StreamReader refuses anything over 64 KiB by
    default. That failure only ever showed up with a phone attached, which is the
    worst place to find out.
    """
    import json, sys

    ITEMS = 20000

    def emit(payload):
        sys.stdout.write(json.dumps(payload) + "\\n")
        sys.stdout.flush()

    for line in sys.stdin:
        message = json.loads(line)
        command, request_id = message.get("cmd"), message.get("id")
        if command == "list":
            emit({"event": "result", "id": request_id, "ok": True, "data": [
                {"identifier": f"202609_a/QSCU{n:05d}.JPG",
                 "original_filename": f"IMG_{n:05d}.HEIC",
                 "size": 2100000 + n, "created": 1756944000.0}
                for n in range(ITEMS)
            ]})
        elif command == "close":
            emit({"event": "result", "id": request_id, "ok": True, "data": None})
        else:
            emit({"event": "result", "id": request_id, "ok": True, "data": None})
    '''
)

SLOW_STUB = textwrap.dedent(
    '''
    """A worker that answers a delete only once it has been told to stop."""
    import json, sys

    def emit(payload):
        sys.stdout.write(json.dumps(payload) + "\\n")
        sys.stdout.flush()

    pending = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        command = message.get("cmd")
        if command == "delete":
            pending = message.get("id")          # deliberately no answer yet
        elif command == "stop" and pending is not None:
            emit({"event": "result", "id": pending, "ok": True, "data": 0})
            pending = None
        elif command == "close":
            emit({"event": "result", "id": message.get("id"), "ok": True, "data": None})
            break
    '''
)


@pytest.fixture
def stub_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the service at a stub worker instead of the Objective-C one."""
    script = tmp_path / "stub_worker.py"
    script.write_text(STUB, encoding="utf-8")
    monkeypatch.setattr(ptp_module, "_worker_command", lambda: [sys.executable, str(script)])
    monkeypatch.setattr(ptp_module, "ptp_is_available", lambda: True)
    return script


async def test_connect_forwards_progress_and_lists_items(stub_worker: Path) -> None:
    seen: list[int] = []
    service = PtpService(device_name="Fake iPhone")
    try:
        await service.connect(seen.append)
        assets = await service.list_assets()
    finally:
        await service.aclose()

    assert seen[0] == 7
    assert [asset.original_filename for asset in assets] == ["IMG_0001.HEIC", "IMG_0002.MOV"]
    assert assets[0].key == ("IMG_0001.HEIC", 1234)
    assert assets[0].created is not None
    assert assets[1].created is None


async def test_an_error_from_the_worker_becomes_a_sentence(stub_worker: Path) -> None:
    service = PtpService()
    try:
        await service.connect()
        with pytest.raises(IpmError) as raised:
            await service._request({"cmd": "explode"})  # noqa: SLF001 - protocol-level test
    finally:
        await service.aclose()

    assert "locked" in str(raised.value)
    assert "Traceback" not in str(raised.value)


async def test_a_worker_that_dies_is_reported_not_hung(stub_worker: Path) -> None:
    service = PtpService()
    try:
        await service.connect()
        with pytest.raises(IpmError) as raised:
            await service._request({"cmd": "vanish"})  # noqa: SLF001 - protocol-level test
    finally:
        await service.aclose()

    assert "stopped unexpectedly" in str(raised.value)


async def test_a_worker_that_goes_quiet_is_given_up_on(
    stub_worker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child that is alive but has stopped answering must not hold the app for ever.

    This is the other half of the hang that cost a session sixty-eight minutes: the
    process had not died -- ``returncode`` was still ``None``, so the death check
    could not fire -- it was simply never going to answer, because something else had
    the phone's photo library open. Before the silence timeout existed, ``_request``
    waited on ``readline()`` with no deadline at all and the only way out was to kill
    the terminal.

    The stub ignores any command it does not recognise, which is exactly the shape of
    the failure: the pipe is open, the child is running, nothing comes back.
    """
    monkeypatch.setattr(ptp_module, "_SILENCE_TIMEOUT", 0.5)

    service = PtpService()
    try:
        await service.connect()
        with pytest.raises(IpmError) as raised:
            # The outer deadline is the test's own safety net, twenty times the one
            # under test. Without it a regression here would not fail the run, it
            # would hang it -- which is the very bug this test is about.
            await asyncio.wait_for(
                service._request({"cmd": "sulk"}),  # noqa: SLF001 - protocol-level test
                timeout=10.0,
            )
    finally:
        await service.aclose()

    message = str(raised.value)
    assert "stopped responding" in message
    # The sentence has to name the usual culprit, because it usually is the culprit.
    assert "Image Capture" in message
    assert "Traceback" not in message


async def test_asking_to_stop_reaches_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled deletion has to be told to the phone, not just to the screen."""
    script = tmp_path / "slow_worker.py"
    script.write_text(SLOW_STUB, encoding="utf-8")
    monkeypatch.setattr(ptp_module, "_worker_command", lambda: [sys.executable, str(script)])
    monkeypatch.setattr(ptp_module, "ptp_is_available", lambda: True)

    service = PtpService()
    await service._spawn()  # noqa: SLF001 - the stub answers no connect
    try:
        await service.delete_assets(["0", "1"], should_stop=lambda: True)
    finally:
        await service.aclose()


async def test_missing_support_is_explained_rather_than_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ptp_module, "ptp_is_available", lambda: False)
    with pytest.raises(IpmError) as raised:
        await PtpService().connect()
    assert "image-capture support" in str(raised.value)


# ---------------------------------------------------------------------------
# The session retry, which needs no framework and no phone
# ---------------------------------------------------------------------------


def test_a_session_that_opens_on_the_second_try_is_not_reported_as_locked() -> None:
    """The behaviour the retry exists for, seen on a real iPhone 13 (iOS 26.5).

    The browser announces the device before iOS has finished making it available,
    so the first ``requestOpenSession`` is refused with the same error a locked
    phone gives -- while the phone is unlocked and visible in Image Capture.
    """
    answers = ["-9943 Please unlock", None]
    waits: list[float] = []

    def attempt() -> str | None:
        return answers.pop(0)

    assert retry_session(attempt, waits.append) is None
    assert waits == [SESSION_RETRY_PAUSE], "it should have paused exactly once"


def test_a_phone_that_never_opens_reports_the_last_error() -> None:
    waits: list[float] = []

    def attempt() -> str | None:
        return "-9943 Please unlock"

    assert retry_session(attempt, waits.append) == "-9943 Please unlock"
    # Every attempt but the last is followed by a pause, and no more than that.
    assert len(waits) == SESSION_ATTEMPTS - 1


def test_a_session_that_opens_at_once_never_pauses() -> None:
    waits: list[float] = []
    assert retry_session(lambda: None, waits.append) is None
    assert waits == []


def test_the_retry_window_gives_a_user_time_to_unlock_the_phone() -> None:
    """A locked phone is told to unlock only after a wait it can be unlocked in."""
    assert SESSION_ATTEMPTS >= 2
    assert (SESSION_ATTEMPTS - 1) * SESSION_RETRY_PAUSE >= 10


async def test_a_catalogue_larger_than_one_read_buffer_arrives_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug that made every real library unusable, pinned without a phone.

    asyncio's StreamReader refuses a line longer than 64 KiB and leaves the rest in
    its buffer, so the failure was not even a clean error -- the next read saw
    nonsense. A 16 932-item iPhone answers with 1.6 MiB on one line; this stub sends
    twenty thousand items, about 2 MiB.
    """
    worker = tmp_path / "big_worker.py"
    worker.write_text(BIG_STUB, encoding="utf-8")
    monkeypatch.setattr(ptp_module, "_worker_command", lambda: [sys.executable, str(worker)])
    # The stub stands in for the worker, so the image-capture support it would need is
    # beside the point -- and absent on the Linux CI job, where this test used to fail.
    monkeypatch.setattr(ptp_module, "ptp_is_available", lambda: True)

    service = PtpService()
    try:
        await service.connect()
        items = await service.list_assets()
    finally:
        await service.aclose()

    assert len(items) == 20000
    assert items[0].original_filename == "IMG_00000.HEIC"
    assert items[-1].original_filename == "IMG_19999.HEIC"
    # And the key the matcher joins on survived the trip.
    assert items[7].key == ("IMG_00007.HEIC", 2100007)


def test_the_read_limit_leaves_room_for_a_real_library() -> None:
    """A guard on the constant, so nobody trims it back towards the default.

    99 bytes an item, measured on a real catalogue; the default asyncio limit of
    64 KiB holds about 660 items, which is a phone nobody owns.
    """
    from ipm.device.ptp import _STREAM_LIMIT

    assert _STREAM_LIMIT >= 16 * 1024 * 1024
    assert _STREAM_LIMIT // 99 > 500_000
