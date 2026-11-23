"""One copy of the app per library.

A second copy started on the same library (a double click while the first was
minimised, or one left running after its window closed) took every running
download as abandoned and ran it too: both wrote the same files and the same
database, which is where "Access is denied" and "database is locked" came from.
"""

import os
from pathlib import Path
from typing import IO, Optional

LOCK = "app.lock"
ADDRESS = "app.address"

_held: Optional[IO[str]] = None


def acquire(folder: Path) -> bool:
    """Take the library in ``folder`` for this process; False when another has it.

    The lock is the operating system's, on an open file: it goes when the
    process does, however it ends, so a crash never leaves the library locked.
    """
    global _held
    if _held is not None:
        return True
    folder.mkdir(parents=True, exist_ok=True)
    handle = open(folder / LOCK, "a+", encoding="utf-8")
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _held = handle
    return True


def release() -> None:
    global _held
    if _held is not None:
        try:
            _held.close()
        except OSError:
            pass
        _held = None


def note_address(folder: Path, url: str) -> None:
    """Say where this copy is answering, for a second one to open."""
    try:
        (folder / ADDRESS).write_text(url, encoding="utf-8")
    except OSError:
        pass


def running_address(folder: Path) -> Optional[str]:
    try:
        url = (folder / ADDRESS).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return url if url.startswith("http://") else None
