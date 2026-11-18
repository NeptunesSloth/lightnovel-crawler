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

import errno
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from threading import Event, Thread
import time
from typing import Any, Callable, Dict, Iterator, List, NamedTuple, Optional, Set, Tuple

MARKER = "library-location.json"
# Left in a folder a move is copying into, so a later try is known to carry on
# from that copy and not to be writing over someone's own folder.
STAMP = ".lncrawl-library"
# What the start that finishes a move did, step by step, in the folder moved to:
# it runs before the app has a window or a log of its own.
MOVE_LOG = "move.log"
# What the stamp says once the app runs from the folder. A folder that says so
# and has a database is somebody's library, not the leftovers of a move.
IN_USE = "Lightnovel Crawler library, in use"
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

# A drive that stops answering part way (an external one going to sleep, a cable
# nudged) is waited for and tried again, for this long without one file getting
# through, before the copy gives up. The waits start short and double.
RETRY_PATIENCE = 900.0
RETRY_FIRST_WAIT = 2.0
RETRY_LONGEST_WAIT = 60.0

Progress = Callable[[int, int], None]  # (files, bytes) since the last call
Report = Callable[[str], None]  # what the switch is doing now, in a few words
# Told the error each time a file could not be copied and is about to be tried
# again, and None once one gets through.
Waiting = Callable[[Optional[OSError]], None]


class LibraryMissing(Exception):
    """The library was moved to a folder that cannot be found now."""


class MoveStopped(Exception):
    """The copy was asked to stop."""


def read_marker(base: Path) -> Dict[str, Any]:
    """What ``base`` says about its library.

    ``path`` with ``state`` "moved": the folder the library is in now.
    ``moving_to`` and ``copied``: a move under way, and whether its copy is done.
    ``cleanup`` and ``old``: the copy still to be removed from the folder it left.
    ``left_behind``: how many files the switch could not bring along.
    ``failed`` and ``failed_target``: why the last start could not finish a move.
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
    _update_marker(base, drop=("cleanup", "old", "left_behind"))


def move_failed(base: Path, target: str, reason: str) -> None:
    """Drop a move the start could not finish, keeping why for the app to show."""
    _update_marker(base, drop=("moving_to", "copied"), failed=reason, failed_target=target)


def failure_shown(base: Path) -> None:
    _update_marker(base, drop=("failed", "failed_target"))


def _note(folder: Path, text: str) -> None:
    """Add a line to the move's log in ``folder``; a log that cannot be written is done without."""
    try:
        with open(folder / MOVE_LOG, "a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {text}\n")
    except OSError:
        pass


class _Notice:
    """A small window saying what the start is busy with, where there is no console.

    The desktop app on Windows carries no GUI toolkit, so this is a bare system
    text box. Nothing depends on it: if it cannot be made, the work goes on unseen.
    """

    def __init__(self, text: str) -> None:
        import ctypes

        self._user32 = user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        user32.CreateWindowExW.restype = ctypes.c_void_p
        width, height = 560, 150
        left = max(0, (int(user32.GetSystemMetrics(0)) - width) // 2)
        top = max(0, (int(user32.GetSystemMetrics(1)) - height) // 2)
        # topmost | a text box, centred, with a border, shown at once
        handle = user32.CreateWindowExW(
            0x00000008,
            "STATIC",
            text,
            0x80000000 | 0x10000000 | 0x00800000 | 0x00000001,
            left,
            top,
            width,
            height,
            None,
            None,
            None,
            None,
        )
        if not handle:
            raise OSError("the window could not be made")
        self._handle = ctypes.c_void_p(handle)
        gdi32 = ctypes.windll.gdi32  # type: ignore[attr-defined]
        gdi32.GetStockObject.restype = ctypes.c_void_p
        font = ctypes.c_void_p(gdi32.GetStockObject(17))  # the font dialogs use
        user32.SendMessageW(self._handle, 0x0030, font, ctypes.c_void_p(1))
        self._text = text
        self._pump()

    def _pump(self) -> None:
        import ctypes
        from ctypes import wintypes

        message = wintypes.MSG()
        while self._user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
            self._user32.TranslateMessage(ctypes.byref(message))
            self._user32.DispatchMessageW(ctypes.byref(message))

    def show(self, text: str) -> None:
        if text != self._text:
            self._user32.SetWindowTextW(self._handle, text)
            self._text = text
        self._pump()

    def close(self) -> None:
        self._user32.DestroyWindow(self._handle)


def _desktop_app() -> bool:
    return os.name == "nt" and bool(getattr(sys, "frozen", False))


def _open_notice(text: str) -> Optional[_Notice]:
    if not _desktop_app():
        return None
    try:
        return _Notice(text)
    except Exception:
        return None


def finish_in_view(work: Callable[[Report], None]) -> None:
    """Run ``work``, showing what it reports: in a small window, or on the console.

    The start that finishes a move snapshots a database of a gigabyte or more
    before the app has a window. With nothing on screen that looked like an app
    that would not open, and was closed or started a second time.
    """
    heading = "Lightnovel Crawler is finishing the library move.\nPlease leave it open."
    state: Dict[str, Any] = {"text": "Starting…", "error": None}

    def report(text: str) -> None:
        state["text"] = text
        if not _desktop_app():
            print(f"Finishing the library move: {text}", file=sys.stderr)

    def run() -> None:
        try:
            work(report)
        except BaseException as error:  # handed back to the caller below
            state["error"] = error

    thread = Thread(target=run, name="library-move-switch")
    thread.start()
    notice = _open_notice(f"\n{heading}\n\n{state['text']}")
    while thread.is_alive():
        thread.join(0.2)
        if notice is not None:
            try:
                notice.show(f"\n{heading}\n\n{state['text']}")
            except Exception:
                notice = None
    if notice is not None:
        try:
            notice.close()
        except Exception:
            pass
    if state["error"] is not None:
        raise state["error"]


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
            finish_in_view(lambda report: switch(base, current, Path(target), report))
            return Path(target)
        except Exception as error:  # the library is whole where it was: use it, and say why
            if Path(target).is_dir():
                _note(Path(target), f"The move was not finished: {error!r}. Still using {current}.")
            # The desktop app has no console, so the reason is kept for its
            # storage card. If even that cannot be written (the drive the app
            # starts on is full), the app still has to start.
            try:
                move_failed(base, target, str(error) or type(error).__name__)
            except OSError:
                pass
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


def holds_library(folder: Path) -> bool:
    """Whether ``folder`` is a library the app has run from, with its database."""
    try:
        stamp = (folder / STAMP).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return stamp.startswith(IN_USE) and (folder / DATABASE).is_file()


def choose_existing(current: Path, folder: Path) -> Path:
    """``folder``, checked to be a library the app can start from in place of ``current``.

    For a library that is already somewhere: on a drive that outlived the
    computer it was made on, say. Nothing is copied or changed in either folder.
    Raises ValueError, with the reason, for a folder that is not one.
    """
    if not folder.is_absolute():
        raise ValueError(f"{folder} is not a full path to a folder")
    if not folder.is_dir():
        raise ValueError(f"{folder} cannot be found")
    if _key(folder) == _key(current):
        raise ValueError(f"The library in {folder} is the one in use now")
    if not (folder / DATABASE).is_file():
        inside = folder / "LNCrawl"
        hint = f"; did you mean {inside}?" if (inside / DATABASE).is_file() else ""
        raise ValueError(f"{folder} has no library in it (no {DATABASE} there){hint}")
    return folder


def drive_roots() -> List[Path]:
    """The drives a library could be on: every drive letter, or what is mounted for the user."""
    if os.name == "nt":
        import string

        return [
            Path(f"{letter}:\\")
            for letter in string.ascii_uppercase
            if os.path.isdir(f"{letter}:\\")
        ]
    roots: List[Path] = []
    for parent in ("/Volumes", "/media", "/mnt", "/run/media"):
        try:
            entries = sorted(os.scandir(parent), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir() and os.path.ismount(entry.path):
                    roots.append(Path(entry.path))
            except OSError:
                continue
    return roots


def _is_library(folder: Path) -> bool:
    try:
        return (folder / DATABASE).is_file() and (
            (folder / STAMP).is_file() or (folder / "novels").is_dir()
        )
    except OSError:
        return False


def find_libraries(roots: List[Path], current: Path, limit: int = 500) -> List[Path]:
    """Libraries on these drives other than ``current``: a drive itself, or a folder at its top.

    So that nobody has to know or type where theirs is. Only the top of each
    drive is looked at (``limit`` folders of it), which is where a move puts one.
    """
    found: List[Path] = []
    here = _key(current)
    for root in roots:
        candidates = [root]
        try:
            with os.scandir(root) as entries:
                for index, entry in enumerate(entries):
                    if index >= limit:
                        break
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            candidates.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
        for folder in candidates:
            if _key(folder) != here and _is_library(folder):
                found.append(folder)
    return found


def point_at(base: Path, folder: Path) -> None:
    """From the next start, use the library in ``folder``. Nothing is moved or removed."""
    if _key(folder) == _key(base):
        write_marker(base)
    else:
        write_marker(base, path=str(folder), state="moved")


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
    if holds_library(target):
        # Moving there would put this library's database over that one's: after
        # a reinstall, an empty library over the one the drive was kept for.
        raise ValueError(
            f"{target} already holds a library. To open that one, use "
            f'"Use the library in this folder"; to replace it, delete the folder first'
        )
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


class Size(NamedTuple):
    files: int
    bytes: int
    on_disk: int  # bytes, with each file rounded up to a whole storage block


def allocation_unit(folder: Path) -> int:
    """The size of the blocks the drive holding ``folder`` stores files in.

    exFAT on a big drive uses 128 KB blocks, and each file takes whole ones: a
    library of 1.67 million page images needs about 100 GB more there than its
    sizes add up to, which is better known before a day-long copy than after.
    """
    try:
        if os.name == "nt":
            import ctypes

            sectors, sector_bytes = ctypes.c_ulong(0), ctypes.c_ulong(0)
            free, total = ctypes.c_ulong(0), ctypes.c_ulong(0)
            root = os.path.splitdrive(os.path.abspath(folder))[0] + os.sep
            done = ctypes.windll.kernel32.GetDiskFreeSpaceW(  # type: ignore[attr-defined]
                ctypes.c_wchar_p(root),
                ctypes.byref(sectors),
                ctypes.byref(sector_bytes),
                ctypes.byref(free),
                ctypes.byref(total),
            )
            unit = sectors.value * sector_bytes.value if done else 0
        else:
            unit = os.statvfs(folder).f_frsize
    except Exception:
        unit = 0
    return unit if unit > 0 else 4096


def measure(
    root: Path,
    skip: Tuple[str, ...],
    progress: Progress,
    stop: Optional[Event] = None,
    unit: int = 1,
) -> Size:
    """What is under ``root``; ``unit`` is the block size of the drive it is to go on."""
    files = size = on_disk = 0
    for _, entry in walk(root, skip):
        if stop is not None and stop.is_set():
            raise MoveStopped()
        files += 1
        length = entry.stat(follow_symlinks=False).st_size
        size += length
        on_disk += -(-length // unit) * unit
        progress(1, length)
    return Size(files, size, on_disk)


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
    waiting: Optional[Waiting] = None,
    left_behind: Optional[List[str]] = None,
    on_left: Optional[Callable[[str, OSError], None]] = None,
) -> None:
    """Copy what is under ``source`` to ``target``, keeping each file's time.

    A file already there with the same size and time is left alone, so a copy
    that was stopped carries on where it was. Each file arrives under another
    name first: one that is half written never passes for a finished copy.

    A file that cannot be copied is tried again after a wait (see
    `RETRY_PATIENCE`): 790 GB to an external drive stopped at 3 GB because the
    drive did not answer for a moment. A full drive is not waited for.

    With ``left_behind``, a file that cannot be copied is not waited for either:
    it is added to that list and the copy goes on to the next one.
    """
    made: Set[Path] = set()

    def copy_one(entry: "os.DirEntry[str]", info: os.stat_result, copy: Path) -> None:
        if copy.parent not in made:
            copy.parent.mkdir(parents=True, exist_ok=True)
            made.add(copy.parent)
        writing = copy.with_name(f"{copy.name}.moving")
        shutil.copy2(entry.path, writing)
        if writing.stat().st_size != info.st_size:
            writing.unlink()
            raise OSError(f"{copy.name} changed while it was being copied")
        os.replace(writing, copy)

    troubled = False
    for relative, entry in walk(source, skip):
        if stop is not None and stop.is_set():
            raise MoveStopped()
        if skip_folders and skip_folders.intersection(relative.split("/")[:-1]):
            continue
        info = entry.stat(follow_symlinks=False)
        copy = target / relative
        if not _same(info, copy):
            began: Optional[float] = None
            pause = RETRY_FIRST_WAIT
            while True:
                try:
                    copy_one(entry, info, copy)
                    break
                except OSError as error:
                    if error.errno == errno.ENOSPC:
                        raise
                    if not os.path.lexists(entry.path):
                        break  # the file itself is gone: there is nothing to copy
                    if left_behind is not None:
                        try:
                            copy.with_name(f"{copy.name}.moving").unlink()
                        except OSError:
                            pass
                        left_behind.append(f"{source.name}/{relative}")
                        if on_left is not None:
                            on_left(f"{source.name}/{relative}", error)
                        break
                    now = time.monotonic()
                    began = now if began is None else began
                    if now - began >= RETRY_PATIENCE:
                        raise
                    troubled = True
                    if waiting is not None:
                        waiting(error)
                    made.clear()  # a drive that comes back may not have the folders
                    if stop is not None:
                        if stop.wait(pause):
                            raise MoveStopped() from error
                    else:
                        time.sleep(pause)
                    pause = min(max(pause, 0.5) * 2, RETRY_LONGEST_WAIT)
            if troubled:
                troubled = False
                if waiting is not None:
                    waiting(None)
        progress(1, info.st_size)


def _snapshot_database(source: Path, target: Path, report: Optional[Report] = None) -> None:
    """A consistent copy of the database, whatever is still writing to it."""

    def progress(status: int, remaining: int, total: int) -> None:
        if report is not None and total > 0:
            report(f"Copying the database: {100 * (total - remaining) // total}%")

    writing = target.with_name(f"{target.name}.moving")
    writing.unlink(missing_ok=True)
    original = sqlite3.connect(str(source))
    try:
        copy = sqlite3.connect(str(writing))
        try:
            with copy:
                original.backup(copy, pages=4096, progress=progress)
        finally:
            copy.close()
    finally:
        original.close()
    for name in _DATABASE_FILES[1:]:  # a journal left there belongs to no database now
        target.with_name(name).unlink(missing_ok=True)
    os.replace(writing, target)


def switch(base: Path, current: Path, target: Path, report: Optional[Report] = None) -> None:
    """Finish a move whose copy is done: copy what was left for last, then use ``target``.

    ``current`` is where the library is until this returns; ``base`` holds the marker.

    The database and the settings have to arrive, or the move is not made. The
    rest (the app window's browser profile, source files, logs) is made again
    when it is missing, and this runs before the app has a window to say it is
    waiting: a file of those that cannot be copied is left behind, not waited
    for. The browser behind the app window can outlive the app and hold its
    profile open, and one such file undid a move of hours of copying.
    """

    def say(text: str) -> None:
        _note(target, text)
        if report is not None:
            report(text)

    if not (target / STAMP).is_file():
        raise OSError("the copy made there is gone")
    say(f"Finishing the move from {current} to {target}")
    if (current / DATABASE).is_file():
        say("Copying the database")
        _snapshot_database(current / DATABASE, target / DATABASE, report)
        say("The database is copied")
    left_behind: List[str] = []

    def left(name: str, error: OSError) -> None:
        _note(target, f"Left behind, in use or unreadable: {name} ({error})")

    for name in LATE:
        if name in _DATABASE_FILES:
            continue
        entry = current / name
        if entry.exists():
            say(f"Copying {name}")
        if entry.is_dir():
            copy_tree(
                entry,
                target / name,
                (),
                lambda files, size: None,
                skip_folders=_CACHES,
                left_behind=left_behind,
                on_left=left,
            )
        elif entry.is_file():
            try:
                shutil.copy2(entry, target / name)
            except OSError as error:
                if name == "config.json" or error.errno == errno.ENOSPC:
                    raise
                left_behind.append(name)
                left(name, error)
    (target / STAMP).write_text(f"{IN_USE} since it was moved from {current}\n", encoding="utf-8")
    extra: Dict[str, Any] = {"left_behind": len(left_behind)} if left_behind else {}
    say(f"Done: the library is now in {target} ({len(left_behind)} file(s) left behind)")
    write_marker(base, path=str(target), state="moved", cleanup=True, old=str(current), **extra)


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
