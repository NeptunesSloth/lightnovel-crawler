"""Moving the library to another drive from the app.

The rules that keep the library whole are in `lncrawl/relocation.py`. This is the
part that runs inside the app: it pauses downloads, makes the copy with progress
to show, and after the restart that switches to the new folder, removes the old
copy.
"""

import logging
import os
from pathlib import Path
import shutil
from threading import Event, Lock, Thread
from typing import Any, Dict, Optional

from ..context import ctx
from ..relocation import (
    LATE,
    SKIPPED,
    STAMP,
    MoveStopped,
    allocation_unit,
    begin_move,
    choose_target,
    cleanup_done,
    copy_done,
    copy_tree,
    failure_shown,
    forget_move,
    measure,
    read_marker,
    remove_old_copy,
)
from ..utils.file_tools import format_size, writable_folder

logger = logging.getLogger(__name__)

# Room wanted on the new drive beyond the library itself, for the downloads
# that follow: a drive filled to the last byte by the move helps nobody.
SPARE = 1024 * 1024 * 1024

_ACTIVE = ("pausing", "measuring", "copying")


def _nothing(files: int, size: int) -> None:
    pass


class LibraryMoveService:
    def __init__(self) -> None:
        self._lock = Lock()
        self._stop = Event()
        # idle, pausing, measuring, copying, copied (waiting for a restart), failed
        self._state = "idle"
        self._target: Optional[Path] = None
        self._files = 0
        self._bytes = 0
        self._total_files = 0
        self._total_bytes = 0
        self._error: Optional[str] = None
        self._problem: Optional[str] = None  # why the copy is waiting to try a file again
        self._paused = False  # downloads were running, and were stopped for the copy
        self._old: Dict[str, Any] = {}  # the old copy being removed after a move
        self._left_behind = 0  # files the switch to this folder could not bring along

    def close(self) -> None:
        self._stop.set()

    def status(self) -> Dict[str, Any]:
        folder = ctx.config.app.app_dir
        try:
            free: Optional[int] = shutil.disk_usage(folder).free
        except OSError:
            free = None
        with self._lock:
            return {
                "folder": str(folder),
                "free": free,
                "state": self._state,
                "target": str(self._target) if self._target else None,
                "files": self._files,
                "bytes": self._bytes,
                "total_files": self._total_files,
                "total_bytes": self._total_bytes,
                "error": self._error,
                "problem": self._problem,
                "old_copy": dict(self._old) or None,
                "left_behind": self._left_behind,
            }

    def start(self, folder: str) -> Dict[str, Any]:
        """Start moving the library to ``folder``. ValueError or OSError says why not."""
        current = ctx.config.app.app_dir
        target = choose_target(current, Path(folder).expanduser())
        writable_folder(str(target))  # made now; refused now if it cannot be written to
        with self._lock:
            if self._state in _ACTIVE or self._state == "copied":
                raise ValueError("A move is already under way")
            if self._old.get("state") == "removing":
                raise ValueError(
                    "The old copy from the last move is still being removed; "
                    "try again when that is done"
                )
            self._state = "pausing"
            self._target = target
            self._error = None
            self._problem = None
            self._files = self._bytes = self._total_files = self._total_bytes = 0
            self._stop = Event()
            stop = self._stop
        Thread(
            target=self._copy,
            args=(current, ctx.config.app.base_dir, target, stop),
            daemon=True,
            name="library-move",
        ).start()
        return self.status()

    def cancel(self) -> Dict[str, Any]:
        """Stop a move. The library stays where it is; what was copied is kept for another try."""
        with self._lock:
            state = self._state
            if state in _ACTIVE:
                self._stop.set()  # the copying thread tidies up and resumes downloads
            elif state == "copied":
                self._state = "idle"
                self._target = None
        if state == "copied":
            forget_move(ctx.config.app.base_dir)
            self._resume_downloads()
        return self.status()

    def on_startup(self) -> None:
        """After the app has started: tidy up after the last run's move."""
        base, current = ctx.config.app.base_dir, ctx.config.app.app_dir
        marker = read_marker(base)
        if marker.get("moving_to"):
            # a copy the last run did not finish: the library is where it was,
            # and what was copied waits there for another try
            forget_move(base)
        failed = marker.get("failed")
        if isinstance(failed, str) and failed:
            # the start could not finish the move: say why, once
            went_to = marker.get("failed_target")
            logger.error(f"The library could not be moved to {went_to}: {failed}")
            with self._lock:
                self._state = "failed"
                self._error = f"it could not be finished when the app started ({failed})"
                self._target = Path(went_to) if isinstance(went_to, str) and went_to else None
            try:
                failure_shown(base)
            except OSError:
                pass
        left = marker.get("left_behind")
        if isinstance(left, int) and left:
            logger.warning(f"{left} file(s) in use could not be brought along with the library")
            with self._lock:
                self._left_behind = left
        old = marker.get("old")
        if marker.get("cleanup") and isinstance(old, str) and old:
            Thread(
                target=self._remove_old,
                args=(base, Path(old), current, self._stop),
                daemon=True,
                name="library-move-cleanup",
            ).start()

    # ------------------------------------------------------------------ #

    def _count(self, files: int, size: int) -> None:
        with self._lock:
            self._files += files
            self._bytes += size

    def _waiting(self, error: Optional[OSError]) -> None:
        """The copy could not write a file and will try again; None once it could."""
        if error is not None and self._problem is None:
            logger.warning(f"Moving the library: waiting to try again after: {error}")
        with self._lock:
            self._problem = str(error) if error is not None else None

    def _resume_downloads(self) -> None:
        with self._lock:
            paused, self._paused = self._paused, False
        if paused:
            ctx.scheduler.start()

    def _copy(self, current: Path, base: Path, target: Path, stop: Event) -> None:
        skip = (*LATE, *SKIPPED)
        try:
            # Downloads write new files while a copy reads the old ones, and a
            # running export is picked up again where it stopped when they resume.
            if ctx.scheduler.running:
                with self._lock:
                    self._paused = True
                ctx.scheduler.stop()
            with self._lock:
                self._state = "measuring"
            # Each file takes whole storage blocks on the new drive, and on a big
            # exFAT drive those are 128 KB: count the room in its blocks.
            unit = allocation_unit(target)
            files, size, on_disk = measure(current, skip, self._count, stop, unit)
            already = 0
            if (target / STAMP).is_file():  # an earlier try: what it copied is not copied again
                already = measure(target, (STAMP,), _nothing, stop, unit).on_disk
            late = sum(self._size(current / name, stop) for name in LATE)
            free = shutil.disk_usage(target).free
            need = max(0, on_disk - already) + late + SPARE
            if free < need:
                blocks = (
                    f" (that drive stores files in {format_size(unit)} blocks, so "
                    f"{files:,} files take more room there)"
                    if on_disk - size > SPARE
                    else ""
                )
                raise OSError(
                    f"{target} has {format_size(free)} free and the library needs "
                    f"{format_size(need)}{blocks}"
                )
            (target / STAMP).write_text(
                f"Lightnovel Crawler library, being moved here from {current}\n", encoding="utf-8"
            )
            begin_move(base, target)
            with self._lock:
                self._state = "copying"
                self._total_files, self._total_bytes = files, size
                self._files = self._bytes = 0
            copy_tree(current, target, skip, self._count, stop, waiting=self._waiting)
            copy_done(base)
            with self._lock:
                self._state = "copied"
                self._problem = None
            logger.info(
                f"Library copied to {target} ({files} files, {format_size(size)}); "
                "the next start uses it"
            )
        except MoveStopped:
            forget_move(base)
            with self._lock:
                self._state = "idle"
                self._target = None
                self._problem = None
            self._resume_downloads()
        except Exception as error:
            logger.exception("Moving the library failed")
            forget_move(base)
            with self._lock:
                self._state = "failed"
                self._error = str(error)
                self._problem = None
            self._resume_downloads()

    @staticmethod
    def _size(entry: Path, stop: Event) -> int:
        if entry.is_dir():
            return measure(entry, (), _nothing, stop).bytes
        try:
            return entry.stat().st_size
        except OSError:
            return 0

    def _remove_old(self, base: Path, old: Path, current: Path, stop: Event) -> None:
        def freed(files: int, size: int) -> None:
            with self._lock:
                self._old["freed"] += size
                self._old["files"] += files

        with self._lock:
            self._old = {"state": "removing", "path": str(old), "freed": 0, "files": 0, "left": 0}
        try:
            _, left = remove_old_copy(old, current, freed, stop)
            cleanup_done(base)
            if old != base:
                try:
                    os.rmdir(old)  # only if nothing is left in it
                except OSError:
                    pass
            with self._lock:
                self._old.update(state="done", left=left)
            logger.info(f"Old copy of the library removed from {old} ({left} file(s) left there)")
        except MoveStopped:
            pass  # the app is closing: the next start carries on
        except Exception as error:
            logger.exception("Removing the old copy of the library failed")
            with self._lock:
                self._old.update(state="failed", error=str(error))
