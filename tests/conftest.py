"""Shared fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ipm.core.database import ManifestDatabase, manifest_path_for


@pytest.fixture
def destination(tmp_path: Path) -> Path:
    """An empty destination root."""
    root = tmp_path / "library"
    root.mkdir()
    return root


@pytest.fixture
def database(destination: Path) -> Iterator[ManifestDatabase]:
    """A manifest living inside the destination root."""
    db = ManifestDatabase(manifest_path_for(destination))
    try:
        yield db
    finally:
        db.close()


@pytest.fixture(autouse=True)
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the tests away from the real ``~/.config`` file."""
    monkeypatch.setenv("IPM_CONFIG_DIR", str(tmp_path / "config"))


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add the flag that the hardware suite needs before it may delete anything.

    It lives here because pytest only reads ``pytest_addoption`` from the root
    conftest, but it means nothing outside ``tests/hardware``.
    """
    parser.addoption(
        "--allow-delete",
        action="store_true",
        default=False,
        help=(
            "Let the hardware suite delete from the iPhone. Without it every "
            "deletion call raises. Requires -m hardware and a configured UDID."
        ),
    )
