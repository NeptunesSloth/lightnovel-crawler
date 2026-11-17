"""Where the data folder is, once the library has been moved to another drive.

The library (chapters, manga pages, e-books, the database) outgrows a system
drive, and the data folder's place was only ever an environment variable. A move
made from the app works without one: the folder the app starts in (``base``)
keeps a small marker saying where the library went, and every start follows it.

A move never leaves the library split between two places:

1. While the app runs, with downloads paused, everything is copied except what
   the running app holds open (`LATE`). The marker says ``copied``.
2. The next start, before anything is opened, copies the rest (the database as
   a snapshot) and the marker says ``moved``: from then on the new folder is used.
3. Only then is the old copy removed, and a file only where the new folder has
   the same one.

Until step 2 the old folder is the library and nothing in it is touched, so a
copy that is interrupted, cancelled or whose drive is unplugged costs nothing.

Only the standard library is used here: `lncrawl.config` imports this to find
the data folder, before anything else is loaded.
"""

import json
import multiprocessing
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from threading import Event
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

MARKER = "library-location.json"
# Left in a folder a move is copying into, so a later try is known to carry on
# from that copy and not to be writing over someone's own folder.
STAMP = ".lncrawl-library"
DATABASE = "sqlite.db"
_DATABASE_FILES = (DATABASE, f"{DATABASE}-wal", f"{DATABASE}-shm")
# Copied when the app next starts: the running app has these open or keeps
# changing them (it fetches source files and saves settings whether or not
# downloads are paused), and a copy of the database taken under it could not be
# trusted.
LATE = (
    *_DATABASE_FILES,
    "config.json",
    "app-browser",
    "scraper",
    "sources",
    "logs",
    "export-crash.log",
)
# Never copied: working files that are made again.
SKIPPED = ("tmp", MARKER, STAMP)
# Browser caches in the app window's profile: large, and rebuilt on demand.
_CACHES = {"Cache", "Code Cache", "GPUCache", "DawnCache", "GrShaderCache", "ShaderCache"}
# Timestamps survive a copy to within a filesystem's resolution (2s on exFAT).
_SAME_TIME = 2.0

Progress = Callable[[int, int], None]  # (files, bytes) since the last call


class LibraryMissing(Exception):
    """The library was moved to a folder that cannot be found now."""


class MoveStopped(Exception):
    """The copy was asked to stop."""


def read_marker(base: Path) -> Dict[str, Any]:
    """What ``base`` says about its library.

    ``path`` with ``state`` "moved": the folder the library is in now.
    ``moving_to`` and ``copied``: a move under way, and whether its copy is done.
    ``cleanup`` and ``old``: the copy still to be removed from the folder it left.
    """
    try:
        data = json.loads((base / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_marker(base: Path, **fields: Any) -> None:
    if not fields:
        try:
            (base / MARKER).unlink()
        except FileNotFoundError:
            pass
        return
    base.mkdir(parents=True, exist_ok=True)
    writing = base / f"{MARKER}.part"
    writing.write_text(json.dumps(fields, indent=2), encoding="utf-8")
    os.replace(writing, base / MARKER)


def _update_marker(base: Path, drop: Tuple[str, ...] = (), **fields: Any) -> None:
    marker = {key: value for key, value in read_marker(base).items() if key not in drop}
    marker.update(fields)
    write_marker(base, **marker)


def library_folder(base: Path) -> Path:
    """The folder the library is in, by ``base``'s marker (whether or not it can be found)."""
    marker = read_marker(base)
    path = marker.get("path")
    if marker.get("state") == "moved" and isinstance(path, str) and path:
        return Path(path)
    return base


def begin_move(base: Path, target: Path) -> None:
    _update_marker(base, moving_to=str(target), copied=False)


def copy_done(base: Path) -> None:
    _update_marker(base, copied=True)


def forget_move(base: Path) -> None:
    """Drop a move that is under way; where the library is now stays as it was."""
    _update_marker(base, drop=("moving_to", "copied"))


def cleanup_done(base: Path) -> None:
    _update_marker(base, drop=("cleanup", "old"))


def resolve(base: Path) -> Path:
    """The data folder to use: ``base``, or the folder its library was moved to.

    A copy that finished while the app last ran is completed here first. If that
    cannot be done the app carries on from where the library still is; once a
    move is complete, a folder that has gone missing is an error, since starting
    from ``base`` would show an empty library and fill the old drive again.
    """
    marker = read_marker(base)
    current = library_folder(base)
    target = marker.get("moving_to")
    # a worker process is started by an app that already knows its folder
    if (
        isinstance(target, str)
        and target
        and marker.get("copied") is True
        and multiprocessing.parent_process() is None
    ):
        try:
            switch(base, current, Path(target))
            return Path(target)
        except Exception as error:  # the library is whole where it was: use it, and say why
            forget_move(base)
            print(
                f"Could not finish moving the library to {target} ({error}); "
                f"still using {current}. Start the move again from the app.",
                file=sys.stderr,
            )
    if current != base and not current.is_dir():
        raise LibraryMissing(
            f"The library was moved to {current}, which cannot be found. "
            f"Connect that drive and start again. (To go back to the old folder, "
            f"delete {base / MARKER}; anything downloaded since the move is not there.)"
        )
    return current


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def choose_target(current: Path, folder: Path) -> Path:
    """Where the library goes when asked to move to ``folder``.

    Into the folder itself when it is new, empty or holds an earlier try at this
    move; into an ``LNCrawl`` folder inside it when it has other things in it (a
    drive's root, say), so the library's files are never mixed in with them.
    Raises ValueError, with the reason, for a place that cannot be used.
    """

    def taken(place: Path) -> bool:
        return place.is_dir() and not (place / STAMP).is_file() and any(place.iterdir())

    if not folder.is_absolute():
        raise ValueError(f"{folder} is not a full path to a folder")
    if folder.exists() and not folder.is_dir():
        raise ValueError(f"{folder} is a file, not a folder")
    target = folder / "LNCrawl" if taken(folder) else folder
    here, there = _key(current), _key(target)
    if here == there or there.startswith(here + os.sep) or here.startswith(there + os.sep):
        raise ValueError(f"The library is already in {current}; choose a folder outside it")
    if target.exists() and (not target.is_dir() or taken(target)):
        raise ValueError(f"{target} already has other files in it")
    return target


# ---------------------------------------------------------------------------- #
# copying
# ---------------------------------------------------------------------------- #


def walk(root: Path, skip: Tuple[str, ...] = ()) -> Iterator[Tuple[str, "os.DirEntry[str]"]]:
    """Every file under ``root`` as (path relative to it, directory entry).

    ``skip`` names entries directly in ``root`` to leave out. Links are not
    followed. One directory read per folder: on Windows the entry already holds
    the file's size and time, so nothing is asked of each file.
    """
    pending: List[Tuple[str, Path]] = [("", root)]
    while pending:
        prefix, folder = pending.pop()
        try:
            entries = list(os.scandir(folder))
        except FileNotFoundError:
            continue
        for entry in entries:
            if not prefix and entry.name in skip:
                continue
            relative = f"{prefix}{entry.name}"
            if entry.is_dir(follow_symlinks=False):
                pending.append((f"{relative}/", Path(entry.path)))
            elif entry.is_file(follow_symlinks=False):
                yield relative, entry


def measure(
    root: Path, skip: Tuple[str, ...], progress: Progress, stop: Optional[Event] = None
) -> Tuple[int, int]:
    """(files, bytes) under ``root``."""
    files = size = 0
    for _, entry in walk(root, skip):
        if stop is not None and stop.is_set():
            raise MoveStopped()
        files += 1
        length = entry.stat(follow_symlinks=False).st_size
        size += length
        progress(1, length)
    return files, size


def _same(source: os.stat_result, copy: Path) -> bool:
    try:
        made = copy.stat()
    except OSError:
        return False
    return made.st_size == source.st_size and abs(made.st_mtime - source.st_mtime) < _SAME_TIME


def copy_tree(
    source: Path,
    target: Path,
    skip: Tuple[str, ...],
    progress: Progress,
    stop: Optional[Event] = None,
    skip_folders: Optional[Set[str]] = None,
) -> None:
    """Copy what is under ``source`` to ``target``, keeping each file's time.

    A file already there with the same size and time is left alone, so a copy
    that was stopped carries on where it was. Each file arrives under another
    name first: one that is half written never passes for a finished copy.
    """
    made: Set[Path] = set()
    for relative, entry in walk(source, skip):
        if stop is not None and stop.is_set():
            raise MoveStopped()
        if skip_folders and skip_folders.intersection(relative.split("/")[:-1]):
            continue
        info = entry.stat(follow_symlinks=False)
        copy = target / relative
        if not _same(info, copy):
            if copy.parent not in made:
                copy.parent.mkdir(parents=True, exist_ok=True)
                made.add(copy.parent)
            writing = copy.with_name(f"{copy.name}.moving")
            shutil.copy2(entry.path, writing)
            if writing.stat().st_size != info.st_size:
                writing.unlink()
                raise OSError(f"{relative} changed while it was being copied")
            os.replace(writing, copy)
        progress(1, info.st_size)


def _snapshot_database(source: Path, target: Path) -> None:
    """A consistent copy of the database, whatever is still writing to it."""
    writing = target.with_name(f"{target.name}.moving")
    writing.unlink(missing_ok=True)
    original = sqlite3.connect(str(source))
    try:
        copy = sqlite3.connect(str(writing))
        try:
            with copy:
                original.backup(copy)
        finally:
            copy.close()
    finally:
        original.close()
    for name in _DATABASE_FILES[1:]:  # a journal left there belongs to no database now
        target.with_name(name).unlink(missing_ok=True)
    os.replace(writing, target)


def switch(base: Path, current: Path, target: Path) -> None:
    """Finish a move whose copy is done: copy what was left for last, then use ``target``.

    ``current`` is where the library is until this returns; ``base`` holds the marker.
    """
    if not (target / STAMP).is_file():
        raise OSError("the copy made there is gone")
    if (current / DATABASE).is_file():
        _snapshot_database(current / DATABASE, target / DATABASE)
    for name in LATE:
        if name in _DATABASE_FILES:
            continue
        entry = current / name
        if entry.is_dir():
            copy_tree(entry, target / name, (), lambda files, size: None, skip_folders=_CACHES)
        elif entry.is_file():
            shutil.copy2(entry, target / name)
    write_marker(base, path=str(target), state="moved", cleanup=True, old=str(current))


# ---------------------------------------------------------------------------- #
# removing the old copy
# ---------------------------------------------------------------------------- #


def remove_old_copy(
    old: Path, new: Path, progress: Progress, stop: Optional[Event] = None
) -> Tuple[int, int]:
    """Delete from ``old`` what ``new`` now holds; (bytes freed, files left).

    A file goes only when the new folder has one of the same size at the same
    place. The exceptions are working files, which are made again, and what was
    copied at the switch (`LATE`): the app has been running from those since, so
    the new ones have moved on and the old ones only need the new to be there.
    """
    here, there = _key(old), _key(new)
    if here == there or there.startswith(here + os.sep) or here.startswith(there + os.sep):
        raise ValueError(f"{old} is not a copy apart from {new}")
    freed = left = 0
    switched = {name for name in LATE if (new / name).exists()}
    if DATABASE in switched:
        switched.update(_DATABASE_FILES)  # its journal files belong to the old database
    for relative, entry in walk(old, (MARKER,)):
        if stop is not None and stop.is_set():
            raise MoveStopped()
        top = relative.split("/", 1)[0]
        size = entry.stat(follow_symlinks=False).st_size
        if top == "tmp" or top in switched:
            gone = True
        else:
            try:
                gone = (new / relative).stat().st_size == size
            except OSError:
                gone = False
        if gone:
            try:
                os.unlink(entry.path)
            except OSError:
                gone = False
        if gone:
            freed += size
            progress(1, size)
        else:
            left += 1
    # folders emptied by that, deepest first; the old folder itself stays, since
    # it may be the one the app starts in and holds the marker
    for folder, _, _ in os.walk(old, topdown=False):
        if Path(folder) != old:
            try:
                os.rmdir(folder)
            except OSError:
                pass
    return freed, left
