"""SHA-256 of a local file, in chunks.

The digest is what turns "the file is the right size" into "the file is the right
file". It is computed twice in this application:

* while a photo is being copied, over the chunks as they arrive from the device --
  free, since the bytes are already in memory (:mod:`ipm.core.importer`);
* when the user asks for a deep verification before deleting, by reading the local
  copy back and comparing (:mod:`ipm.core.deleter`). That one costs a full read of
  the library, which is exactly why it is a choice and not a silent default.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

__all__ = ["DIGEST_CHUNK", "digest_of"]

DIGEST_CHUNK = 1024 * 1024
"""Read size. Large enough that the syscall overhead disappears, small enough that
progress stays smooth on a 4 GB video."""


def digest_of(path: Path, on_bytes: Callable[[int], None] | None = None) -> str:
    """Return the hex SHA-256 of *path*.

    :param path: File to read.
    :param on_bytes: Called with the size of each chunk read. It may raise to abort
        the hashing -- that is how a long verification stays cancellable.
    :return: 64 hexadecimal characters.
    :raises OSError: if the file cannot be read.
    """
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(DIGEST_CHUNK)
            if not chunk:
                break
            hasher.update(chunk)
            if on_bytes is not None:
                on_bytes(len(chunk))
    return hasher.hexdigest()
