"""Small humanising helpers used by the UI (and unit-tested here, not there)."""

from __future__ import annotations

__all__ = ["human_bytes", "human_duration", "human_rate"]

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def human_bytes(size: float | None) -> str:
    """Format a byte count with binary units, e.g. ``1.4 GB``.

    :param size: Number of bytes, or ``None`` when unknown.
    :return: A short string; ``"—"`` when *size* is ``None``.
    """
    if size is None:
        return "—"
    value = float(size)
    if value < 0:
        return "—"
    index = 0
    while value >= 1024 and index < len(_UNITS) - 1:
        value /= 1024
        index += 1
    if index == 0:
        return f"{int(value)} {_UNITS[index]}"
    precision = 1 if value >= 10 else 2
    return f"{value:.{precision}f} {_UNITS[index]}"


def human_rate(bytes_per_second: float | None) -> str:
    """Format a transfer speed, e.g. ``12.3 MB/s``."""
    if bytes_per_second is None or bytes_per_second <= 0:
        return "—"
    return f"{human_bytes(bytes_per_second)}/s"


def human_duration(seconds: float | None) -> str:
    """Format a duration as ``h:mm:ss`` / ``m:ss``; ``"—"`` when unknown."""
    if seconds is None or seconds < 0 or seconds != seconds or seconds == float("inf"):
        return "—"
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
