"""Translation of low-level exceptions into messages a non-technical user can act on.

The TUI never shows a traceback for an expected condition (locked phone, trust not
granted, cable pulled, disk full). It shows the sentence produced here instead, and
logs the original exception to the optional log file.
"""

from __future__ import annotations

import errno

__all__ = ["IpmError", "friendly_message"]


class IpmError(Exception):
    """An error already phrased for the end user; shown verbatim by the UI."""


_LOCKDOWN_HINTS: tuple[tuple[str, str], ...] = (
    # Matched against the exception class name, so the module never has to import
    # pymobiledevice3 (keeping this file importable in tests without the library).
    (
        "PasswordRequiredError",
        "The iPhone is locked. Unlock it with your passcode and try again.",
    ),
    (
        "PasscodeRequiredError",
        "The iPhone is locked. Unlock it with your passcode and try again.",
    ),
    (
        "PairingDialogResponsePendingError",
        'The iPhone is waiting for your answer. Tap "Trust" on the phone, then try again.',
    ),
    (
        "UserDeniedPairingError",
        'Trust was denied on the iPhone. Unplug the cable, plug it back in and tap "Trust".',
    ),
    (
        "NotTrustedError",
        'This Mac is not trusted by the iPhone. Unlock the phone and tap "Trust" when asked.',
    ),
    (
        "NotPairedError",
        'This Mac is not paired with the iPhone. Unlock the phone and tap "Trust" when asked.',
    ),
    (
        "InvalidHostIDError",
        "The pairing between this Mac and the iPhone is stale. Unplug and replug the cable, "
        'then tap "Trust" on the phone.',
    ),
    (
        "ConnectionFailedToUsbmuxdError",
        "macOS cannot talk to connected iOS devices right now. Make sure the phone is plugged in "
        "with a data cable, then try again.",
    ),
    (
        "NoDeviceConnectedError",
        "No iPhone is connected. Plug it in with a USB cable and unlock it.",
    ),
    (
        "DeviceNotFoundError",
        "The iPhone is no longer reachable. Reconnect the cable and try again.",
    ),
    (
        "ConnectionTerminatedError",
        "The connection to the iPhone was interrupted. Reconnect the cable and press the button "
        "again to resume.",
    ),
    (
        "MuxException",
        "The connection to the iPhone was interrupted. Reconnect the cable and press the button "
        "again to resume.",
    ),
    (
        "AfcFileNotFoundError",
        "A file disappeared from the iPhone while it was being read. It was skipped.",
    ),
    (
        "AfcException",
        "The iPhone refused a file operation. Make sure the phone is unlocked and try again.",
    ),
    (
        "LockdownError",
        "The iPhone refused the connection. Unlock it, confirm the trust prompt and try again.",
    ),
)

_ERRNO_MESSAGES: dict[int, str] = {
    errno.ENOSPC: "This Mac has run out of disk space. Free some space and press the button again "
    "to resume.",
    errno.EACCES: "This Mac denied write access to the destination folder. Pick another folder or "
    "fix its permissions.",
    errno.EPERM: "This Mac denied write access to the destination folder. Pick another folder or "
    "fix its permissions.",
    errno.EROFS: "The destination folder is read-only. Pick a different folder.",
    errno.ENOENT: "The destination folder no longer exists. Pick a different folder.",
    errno.EDQUOT: "The disk quota for the destination folder is exhausted. Free some space and try "
    "again.",
}


def friendly_message(exc: BaseException) -> str:
    """Return a short, actionable, user-facing sentence describing *exc*.

    Falls back to the exception type and text when nothing more specific is known,
    so the user always sees a single line instead of a traceback.

    :param exc: The exception to describe.
    :return: One sentence, safe to print in the UI.
    """
    if isinstance(exc, IpmError):
        return str(exc)

    if isinstance(exc, asyncio_cancelled_types()):
        return "The operation was stopped."

    if isinstance(exc, OSError) and exc.errno in _ERRNO_MESSAGES:
        return _ERRNO_MESSAGES[exc.errno]

    names = {klass.__name__ for klass in type(exc).__mro__}
    for name, message in _LOCKDOWN_HINTS:
        if name in names:
            return message

    if isinstance(exc, OSError):
        detail = exc.strerror or str(exc) or type(exc).__name__
        return f"A file system error occurred: {detail}."

    detail = str(exc).strip()
    return f"Unexpected error ({type(exc).__name__}){f': {detail}' if detail else ''}."


def asyncio_cancelled_types() -> tuple[type[BaseException], ...]:
    """Return the exception types that mean "the user or the app stopped this".

    Kept as a function so :mod:`asyncio` is imported lazily and this module stays
    cheap to import from tests.
    """
    import asyncio

    return (asyncio.CancelledError,)
