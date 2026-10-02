"""Persistent user configuration (destination folder, concurrency, log file).

Stored as a small JSON file so a user can read and edit it by hand. The location
follows the XDG convention and can be overridden with ``IPM_CONFIG_DIR`` (used by
the test suite).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

__all__ = ["Config", "config_path", "load_config", "save_config"]

DEFAULT_CONCURRENCY = 3
MAX_CONCURRENCY = 8


def config_dir() -> Path:
    """Return the directory holding ``config.json``."""
    override = os.environ.get("IPM_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "iphone-photo-manager"


def config_path() -> Path:
    """Return the full path of the configuration file."""
    return config_dir() / "config.json"


@dataclass(frozen=True, slots=True)
class Config:
    """User settings that survive across runs.

    :ivar destination: Root folder for imported media; ``None`` until the user picks one.
    :ivar concurrency: Number of parallel AFC downloads (clamped to 1..``MAX_CONCURRENCY``).
    :ivar log_file: Optional debug log path; ``None`` means logging stays off.
    :ivar permanent_delete_warning_seen: Whether the "this does not go to Recently
        Deleted" notice has been dismissed for good. It is shown before the first
        deletion and never again once the user ticks the box, which is why it has to
        survive a restart.
    """

    destination: Path | None = None
    concurrency: int = DEFAULT_CONCURRENCY
    log_file: Path | None = None
    permanent_delete_warning_seen: bool = False

    def with_destination(self, destination: Path) -> Config:
        """Return a copy pointing at *destination* (expanded and made absolute)."""
        return replace(self, destination=normalise_destination(destination))

    def with_permanent_delete_warning_seen(self) -> Config:
        """Return a copy that will not show the permanence warning again."""
        return replace(self, permanent_delete_warning_seen=True)

    def to_json(self) -> dict[str, Any]:
        """Serialise to plain JSON-compatible types."""
        data = asdict(self)
        data["destination"] = str(self.destination) if self.destination else None
        data["log_file"] = str(self.log_file) if self.log_file else None
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Config:
        """Rebuild a :class:`Config` from parsed JSON, ignoring unknown keys."""
        destination = data.get("destination")
        log_file = data.get("log_file")
        raw_concurrency = data.get("concurrency", DEFAULT_CONCURRENCY)
        try:
            concurrency = int(raw_concurrency)
        except (TypeError, ValueError):
            concurrency = DEFAULT_CONCURRENCY
        return cls(
            destination=normalise_destination(Path(destination)) if destination else None,
            concurrency=clamp_concurrency(concurrency),
            log_file=Path(log_file).expanduser() if log_file else None,
            # Absent in files written by older versions, which simply means the
            # notice has not been dismissed yet -- the safe default for a warning.
            # An older key, `unlock_reminder_seen`, is deliberately not honoured: it
            # dismissed a different notice, about a problem that turned out not to
            # exist, and consent to that is not consent to this.
            permanent_delete_warning_seen=bool(
                data.get("permanent_delete_warning_seen", False)
            ),
        )


def clamp_concurrency(value: int) -> int:
    """Keep the parallel-download count inside a range the device tolerates."""
    return max(1, min(MAX_CONCURRENCY, value))


def normalise_destination(path: Path) -> Path:
    """Expand ``~`` and make *path* absolute without requiring it to exist."""
    expanded = Path(path).expanduser()
    return expanded if expanded.is_absolute() else Path.cwd() / expanded


def load_config(path: Path | None = None) -> Config:
    """Read the configuration file, returning defaults when it is missing or broken.

    A corrupt file is never fatal: the user gets defaults and is asked for the
    destination folder again.
    """
    target = path or config_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Config()
    except OSError:
        return Config()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return Config()
    if not isinstance(parsed, dict):
        return Config()
    return Config.from_json(parsed)


def save_config(config: Config, path: Path | None = None) -> None:
    """Write *config* atomically (temp file + rename) so it can never be truncated."""
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(config.to_json(), indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, target)
