"""The reset script deletes things, so it gets tested like anything else that does.

``scripts/reset-test-state.sh`` exists to put a test machine back to "never ran
this app". It is pointed at the maintainer's photo folder, which is the whole
reason its blast radius has to be provable: inside the destination it may only
remove four-digit year folders, ``*.ipm-part`` files and ``.ipm/`` -- the exact
set the app creates -- and nothing else.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "reset-test-state.sh"
BASH = shutil.which("bash") or "/bin/bash"
"""Resolved once: the S607 lint is right that a bare "bash" is a loose thing to run."""


@pytest.fixture
def library(tmp_path: Path) -> Path:
    """A destination folder as the app would leave it, plus files it never made."""
    root = tmp_path / "library"
    (root / "2024" / "03").mkdir(parents=True)
    (root / "2024" / "03" / "IMG_0001.HEIC").write_bytes(b"photo")
    (root / "2024" / "03" / "IMG_0002.HEIC.ipm-part").write_bytes(b"partial")
    (root / "2025" / "01").mkdir(parents=True)
    (root / "2025" / "01" / "IMG_0003.HEIC").write_bytes(b"photo")
    (root / ".ipm").mkdir()
    (root / ".ipm" / "manifest.db").write_bytes(b"sqlite")

    # Things the user keeps there and the app has never heard of.
    (root / "notes.txt").write_text("mine")
    (root / "Scans").mkdir()
    (root / "Scans" / "holidays.jpg").write_bytes(b"scan")
    return root


def _run(*args: str, expect_ok: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(  # noqa: S603 - fixed interpreter, script from this repo
        [BASH, str(SCRIPT), *args], capture_output=True, text=True, check=False
    )
    if expect_ok:
        assert result.returncode == 0, result.stderr
    return result


def _state(tmp_path: Path) -> list[str]:
    """Env-ish arguments keeping the script away from the real config and log."""
    return ["--log-file", str(tmp_path / "state" / "ipm.log")]


def test_it_removes_exactly_what_the_app_created(library: Path, tmp_path: Path) -> None:
    config = tmp_path / "config" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text(f'{{\n  "destination": "{library}"\n}}\n')
    log = tmp_path / "state" / "ipm.log"
    log.parent.mkdir(parents=True)
    log.write_text("noise")

    _run("--dest", str(library), "--yes", "--log-file", str(log))

    assert not (library / "2024").exists()
    assert not (library / "2025").exists()
    assert not (library / ".ipm").exists()
    # And everything that was not the app's is untouched.
    assert (library / "notes.txt").read_text() == "mine"
    assert (library / "Scans" / "holidays.jpg").exists()
    assert not log.exists()


def test_dry_run_changes_nothing(library: Path, tmp_path: Path) -> None:
    result = _run("--dest", str(library), "--dry-run", *_state(tmp_path))

    assert "2024/" in result.stdout
    assert "nothing was removed" in result.stdout
    assert (library / "2024" / "03" / "IMG_0001.HEIC").exists()
    assert (library / ".ipm" / "manifest.db").exists()


def test_keep_photos_leaves_the_photos(library: Path, tmp_path: Path) -> None:
    _run("--dest", str(library), "--keep-photos", "--yes", *_state(tmp_path))

    assert (library / "2024" / "03" / "IMG_0001.HEIC").exists()
    assert not (library / ".ipm").exists()  # the history is gone, the bytes are not


def test_it_refuses_a_folder_it_did_not_create(tmp_path: Path) -> None:
    stranger = tmp_path / "my-real-archive"
    (stranger / "2024").mkdir(parents=True)
    (stranger / "2024" / "wedding.jpg").write_bytes(b"irreplaceable")

    result = _run("--dest", str(stranger), "--yes", *_state(tmp_path), expect_ok=False)

    assert result.returncode != 0
    assert "no .ipm/manifest.db" in result.stderr
    assert (stranger / "2024" / "wedding.jpg").exists()


def test_it_refuses_home_and_root(tmp_path: Path) -> None:
    """The two paths where a wrong `--dest` would be a catastrophe.

    ``--dry-run`` is passed as a second belt: the guard runs before anything is
    collected, so if it ever stopped working this test fails instead of taking the
    home folder with it.
    """
    for target in ("/", str(Path.home()), "~", "~/"):
        result = _run(
            "--dest", target, "--dry-run", "--yes", *_state(tmp_path), expect_ok=False
        )
        assert result.returncode != 0, f"{target} was not refused"
        assert "refusing to clean" in result.stderr


def test_a_clean_slate_is_not_an_error(tmp_path: Path) -> None:
    empty = tmp_path / "fresh"
    empty.mkdir()

    result = _run("--dest", str(empty), "--yes", *_state(tmp_path))

    assert "already a clean slate" in result.stdout


def test_confirmation_is_required_without_yes(library: Path, tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603 - fixed interpreter, script from this repo
        [BASH, str(SCRIPT), "--dest", str(library), *_state(tmp_path)],
        input="nope\n",
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "Cancelled" in result.stdout
    assert (library / ".ipm" / "manifest.db").exists()


def test_the_photos_are_announced_as_photos(library: Path, tmp_path: Path) -> None:
    """The output was once misread as "only the app's own files would go"."""
    result = _run("--dest", str(library), "--dry-run", *_state(tmp_path))

    assert "PHOTOS AND VIDEOS THAT WOULD BE DELETED" in result.stdout
    assert "cannot be recovered" in result.stdout
    assert "--keep-photos" in result.stdout
    assert "2 files" in result.stdout  # the two .HEIC copies, counted as a total


def test_keep_photos_says_so_instead(library: Path, tmp_path: Path) -> None:
    result = _run("--dest", str(library), "--keep-photos", "--dry-run", *_state(tmp_path))

    assert "Photos and videos: KEPT" in result.stdout
    assert "WOULD BE DELETED" not in result.stdout


def test_the_prompt_repeats_what_is_at_stake(library: Path, tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603 - fixed interpreter, script from this repo
        [BASH, str(SCRIPT), "--dest", str(library), *_state(tmp_path)],
        input="nope\n",
        capture_output=True,
        text=True,
        check=False,
    )

    assert "to DELETE 2 photos and videos" in result.stdout
    assert (library / "2024" / "03" / "IMG_0001.HEIC").exists()
