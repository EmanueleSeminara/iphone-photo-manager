"""The plain-text copy of the activity panel, and its rotation.

Worth its own tests because of what it must never do: a destination the user cannot
write to has to cost them the log, not the import.
"""

from __future__ import annotations

from pathlib import Path

from ipm.core.runlog import LOG_NAME, RunLog


def test_it_writes_what_it_is_given(tmp_path: Path) -> None:
    log = RunLog(tmp_path / LOG_NAME)
    log.open("header line")
    log.write("first")
    log.write("second")
    log.close()

    lines = (tmp_path / LOG_NAME).read_text(encoding="utf-8").splitlines()
    assert lines[0].endswith("header line")
    assert lines[1:] == ["first", "second"]


def test_each_line_is_on_disk_before_the_next_one_is_written(tmp_path: Path) -> None:
    """Flushed every time: the run worth reading is the one that ended badly."""
    path = tmp_path / LOG_NAME
    log = RunLog(path)
    log.open()
    log.write("halfway")
    assert "halfway" in path.read_text(encoding="utf-8")
    log.close()


def test_the_previous_runs_are_kept_and_the_oldest_falls_off(tmp_path: Path) -> None:
    path = tmp_path / LOG_NAME
    for run in range(5):
        log = RunLog(path, keep=3)
        log.open()
        log.write(f"run {run}")
        log.close()

    assert "run 4" in path.read_text(encoding="utf-8")
    assert "run 3" in path.with_name(f"{LOG_NAME}.1").read_text(encoding="utf-8")
    assert "run 2" in path.with_name(f"{LOG_NAME}.2").read_text(encoding="utf-8")
    # keep=3 means three files in total, so the fourth-oldest is gone.
    assert not path.with_name(f"{LOG_NAME}.3").exists()


def test_the_folder_is_created_if_it_is_not_there(tmp_path: Path) -> None:
    log = RunLog(tmp_path / ".ipm" / LOG_NAME)
    log.open()
    log.write("hello")
    log.close()
    assert (tmp_path / ".ipm" / LOG_NAME).read_text(encoding="utf-8").endswith("hello\n")


def test_a_folder_it_cannot_write_to_is_not_fatal(tmp_path: Path) -> None:
    """Losing the log is a nuisance; refusing to copy photos over it would not be."""
    blocker = tmp_path / "a-file"
    blocker.write_text("not a folder")

    log = RunLog(blocker / "sub" / LOG_NAME)
    log.open()  # must not raise
    log.write("swallowed")
    log.close()
    assert not (blocker / "sub").exists()


def test_writing_before_opening_is_ignored(tmp_path: Path) -> None:
    log = RunLog(tmp_path / LOG_NAME)
    log.write("nowhere")
    assert not (tmp_path / LOG_NAME).exists()


def test_it_can_be_used_as_a_context_manager(tmp_path: Path) -> None:
    with RunLog(tmp_path / LOG_NAME) as log:
        log.write("inside")
    assert "inside" in (tmp_path / LOG_NAME).read_text(encoding="utf-8")
