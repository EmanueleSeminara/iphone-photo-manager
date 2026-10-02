"""Configuration, formatting, model mapping, error messages and the CLI parser."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from ipm.cli import _apply_overrides, build_parser
from ipm.config import Config, clamp_concurrency, load_config, save_config
from ipm.core.formatting import human_bytes, human_duration, human_rate
from ipm.core.product_types import marketing_name
from ipm.errors import IpmError, friendly_message
from ipm.system import DiskSpace, disk_space

# -- config ---------------------------------------------------------------


def test_config_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    config = Config(destination=tmp_path / "photos", concurrency=4)
    save_config(config, path)
    assert load_config(path) == config


def test_missing_config_returns_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "nope.json")
    assert config.destination is None
    assert config.concurrency == 3


def test_corrupt_config_returns_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_config(path) == Config()


def test_config_clamps_concurrency(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"concurrency": 999}', encoding="utf-8")
    assert load_config(path).concurrency == 8
    assert clamp_concurrency(0) == 1


def test_with_destination_expands_user(tmp_path: Path) -> None:
    config = Config().with_destination(Path("~/Pictures/iPhone"))
    assert config.destination is not None
    assert config.destination.is_absolute()
    assert "~" not in str(config.destination)


def test_save_config_is_atomic(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save_config(Config(destination=tmp_path / "a"), path)
    save_config(Config(destination=tmp_path / "b"), path)
    assert list(tmp_path.glob("*.tmp")) == []
    assert load_config(path).destination == tmp_path / "b"


# -- formatting -----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "—"),
        (0, "0 B"),
        (512, "512 B"),
        (1024, "1.00 KB"),
        (1536, "1.50 KB"),
        (1024**3, "1.00 GB"),
        (15 * 1024**3, "15.0 GB"),
    ],
)
def test_human_bytes(value: int | None, expected: str) -> None:
    assert human_bytes(value) == expected


def test_human_rate_and_duration() -> None:
    assert human_rate(None) == "—"
    assert human_rate(0) == "—"
    assert human_rate(1024) == "1.00 KB/s"
    assert human_duration(None) == "—"
    assert human_duration(45) == "0:45"
    assert human_duration(125) == "2:05"
    assert human_duration(3725) == "1:02:05"


# -- product types --------------------------------------------------------


def test_marketing_name() -> None:
    assert marketing_name("iPhone14,5") == "iPhone 13"
    assert marketing_name("iPhone17,1") == "iPhone 16 Pro"
    assert marketing_name("iPhone99,9") == "iPhone99,9"
    assert marketing_name(None) is None
    assert marketing_name("") is None


# -- error messages -------------------------------------------------------


def test_friendly_message_passes_through_ipm_error() -> None:
    assert friendly_message(IpmError("Plug the cable back in.")) == "Plug the cable back in."


def test_friendly_message_for_disk_full() -> None:
    exc = OSError(errno.ENOSPC, "No space left on device")
    assert "disk space" in friendly_message(exc)


def test_friendly_message_matches_by_class_name() -> None:
    class PasswordRequiredError(Exception):
        pass

    assert "locked" in friendly_message(PasswordRequiredError())


def test_friendly_message_never_returns_a_traceback() -> None:
    message = friendly_message(ValueError("boom"))
    assert message.startswith("Unexpected error (ValueError)")
    assert "\n" not in message


# -- CLI ------------------------------------------------------------------


def test_parser_has_no_subcommands() -> None:
    parser = build_parser()
    args = parser.parse_args([])
    assert args.dest is None
    assert args.concurrency is None


def test_cli_overrides_are_applied(tmp_path: Path) -> None:
    args = build_parser().parse_args(["--dest", str(tmp_path / "pics"), "-j", "5"])
    config, changed = _apply_overrides(Config(), args)
    assert changed is True
    assert config.destination == tmp_path / "pics"
    assert config.concurrency == 5


def test_cli_log_flag_sets_a_log_file() -> None:
    args = build_parser().parse_args(["--log"])
    config, changed = _apply_overrides(Config(), args)
    assert changed is False
    assert config.log_file is not None


def test_disk_space_reports_the_volume_holding_a_folder(tmp_path: Path) -> None:
    space = disk_space(tmp_path)
    assert space is not None
    assert space.total > 0
    assert 0 <= space.free <= space.total
    assert space.used == space.total - space.free


def test_disk_space_answers_for_a_folder_that_does_not_exist_yet(tmp_path: Path) -> None:
    """A destination is asked about before it is created, so walk up to a real parent."""
    space = disk_space(tmp_path / "not" / "created" / "yet")
    assert space is not None
    assert space.total > 0


def test_an_import_that_would_fill_the_disk_does_not_fit() -> None:
    """The margin is the point: a full startup disk is its own kind of disaster."""
    space = DiskSpace(free=10 * 1024**3, total=100 * 1024**3)
    assert space.fits(1 * 1024**3) is True
    # 9 GB would leave 1 GB, under the 2 GB the check insists on keeping.
    assert space.fits(9 * 1024**3) is False
    assert space.fits(9 * 1024**3, margin=0) is True
