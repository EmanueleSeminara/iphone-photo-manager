"""Walk logic of the AFC backend, driven by a stub AFC service.

The transport itself still needs a real phone, but the traversal rules -- which
roots are scanned, what is skipped, how stats become :class:`RemoteFile` -- are
plain logic and are pinned here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from ipm.device.afc import CPL_ROOT, DCIM_ROOT, AfcDeviceBackend

STAMP = datetime(2024, 3, 14, 9, 0, 0)


class StubAfc:
    """Minimal in-memory stand-in for ``AfcService``."""

    def __init__(self, tree: dict[str, list[str]], stats: dict[str, dict[str, Any]]) -> None:
        self.tree = tree
        self.stats = stats
        self.listed: list[str] = []

    async def listdir(self, path: str) -> list[str]:
        self.listed.append(path)
        if path not in self.tree:
            raise FileNotFoundError(path)
        return list(self.tree[path])

    async def stat(self, path: str) -> dict[str, Any]:
        if path not in self.stats:
            raise FileNotFoundError(path)
        return self.stats[path]

    async def isdir(self, path: str) -> bool:
        stat = await self.stat(path)
        return stat.get("st_ifmt") == "S_IFDIR"


def directory() -> dict[str, Any]:
    return {"st_ifmt": "S_IFDIR", "st_size": 0, "st_mtime": STAMP, "st_birthtime": STAMP}


def regular(size: int = 100) -> dict[str, Any]:
    return {"st_ifmt": "S_IFREG", "st_size": size, "st_mtime": STAMP, "st_birthtime": STAMP}


def build(tree: dict[str, list[str]], stats: dict[str, dict[str, Any]]) -> AfcDeviceBackend:
    return AfcDeviceBackend(lockdown=None, afc=StubAfc(tree, stats), serial="STUB")  # type: ignore[arg-type]


async def test_scans_dcim_recursively() -> None:
    backend = build(
        {
            DCIM_ROOT: ["100APPLE", "101APPLE"],
            f"{DCIM_ROOT}/100APPLE": ["IMG_0001.HEIC", "IMG_0001.MOV"],
            f"{DCIM_ROOT}/101APPLE": ["IMG_0002.JPG"],
        },
        {
            DCIM_ROOT: directory(),
            f"{DCIM_ROOT}/100APPLE": directory(),
            f"{DCIM_ROOT}/101APPLE": directory(),
            f"{DCIM_ROOT}/100APPLE/IMG_0001.HEIC": regular(10),
            f"{DCIM_ROOT}/100APPLE/IMG_0001.MOV": regular(20),
            f"{DCIM_ROOT}/101APPLE/IMG_0002.JPG": regular(30),
        },
    )
    files = await backend.list_media()
    assert [item.path for item in files] == [
        f"{DCIM_ROOT}/100APPLE/IMG_0001.HEIC",
        f"{DCIM_ROOT}/100APPLE/IMG_0001.MOV",
        f"{DCIM_ROOT}/101APPLE/IMG_0002.JPG",
    ]
    assert [item.size for item in files] == [10, 20, 30]


async def test_cpl_assets_are_included() -> None:
    backend = build(
        {
            DCIM_ROOT: ["IMG_0001.HEIC"],
            CPL_ROOT: ["group118"],
            f"{CPL_ROOT}/group118": ["C3C25836.JPG"],
        },
        {
            DCIM_ROOT: directory(),
            f"{DCIM_ROOT}/IMG_0001.HEIC": regular(),
            CPL_ROOT: directory(),
            f"{CPL_ROOT}/group118": directory(),
            f"{CPL_ROOT}/group118/C3C25836.JPG": regular(),
        },
    )
    paths = [item.path for item in await backend.list_media()]
    assert f"{CPL_ROOT}/group118/C3C25836.JPG" in paths
    assert len(paths) == 2


async def test_missing_cpl_root_is_not_an_error() -> None:
    backend = build(
        {DCIM_ROOT: ["IMG_0001.HEIC"]},
        {DCIM_ROOT: directory(), f"{DCIM_ROOT}/IMG_0001.HEIC": regular()},
    )
    files = await backend.list_media()
    assert len(files) == 1


async def test_dot_entries_symlinks_and_unreadable_folders_are_skipped() -> None:
    backend = build(
        {
            DCIM_ROOT: [".MISC", "100APPLE", "BROKEN"],
            f"{DCIM_ROOT}/100APPLE": ["IMG_0001.HEIC", ".hidden.JPG", "link.HEIC"],
        },
        {
            DCIM_ROOT: directory(),
            f"{DCIM_ROOT}/100APPLE": directory(),
            f"{DCIM_ROOT}/BROKEN": directory(),  # listdir will raise: no tree entry
            f"{DCIM_ROOT}/100APPLE/IMG_0001.HEIC": regular(),
            f"{DCIM_ROOT}/100APPLE/.hidden.JPG": regular(),
            f"{DCIM_ROOT}/100APPLE/link.HEIC": {"st_ifmt": "S_IFLNK", "st_size": 0},
        },
    )
    paths = [item.path for item in await backend.list_media()]
    assert paths == [f"{DCIM_ROOT}/100APPLE/IMG_0001.HEIC"]


async def test_entry_that_vanishes_mid_scan_is_skipped() -> None:
    backend = build(
        {DCIM_ROOT: ["GONE.HEIC", "IMG_0001.HEIC"]},
        {DCIM_ROOT: directory(), f"{DCIM_ROOT}/IMG_0001.HEIC": regular()},
    )
    paths = [item.path for item in await backend.list_media()]
    assert paths == [f"{DCIM_ROOT}/IMG_0001.HEIC"]


async def test_progress_callback_reports_running_totals() -> None:
    backend = build(
        {DCIM_ROOT: ["A.HEIC", "B.HEIC"]},
        {
            DCIM_ROOT: directory(),
            f"{DCIM_ROOT}/A.HEIC": regular(100),
            f"{DCIM_ROOT}/B.HEIC": regular(50),
        },
    )
    seen: list[tuple[int, int]] = []
    await backend.list_media(on_progress=lambda count, size: seen.append((count, size)))
    assert seen == [(1, 100), (2, 150)]


@pytest.mark.parametrize("field", ["st_birthtime", "st_mtime"])
async def test_timestamps_come_from_the_device(field: str) -> None:
    stat = regular()
    stat[field] = datetime(2019, 7, 4, 8, 30)
    backend = build({DCIM_ROOT: ["A.HEIC"]}, {DCIM_ROOT: directory(), f"{DCIM_ROOT}/A.HEIC": stat})
    files = await backend.list_media()
    assert getattr(files[0], "created" if field == "st_birthtime" else "modified") == datetime(
        2019, 7, 4, 8, 30
    )
