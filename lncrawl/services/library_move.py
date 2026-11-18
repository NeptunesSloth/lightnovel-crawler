"""Moving the library to another drive from the app, in one go.

The rules that keep the library whole are in `lncrawl/relocation.py`. This is the
part that runs inside the app. It pauses downloads and copies everything while
the app goes on being used. Then it takes the database to itself for a moment,
long enough to bring the copy up to date, and from there on the whole app uses
the new folder: the database, the settings, the files it serves and writes, the
log. Downloads carry on, and the old copy is removed. Nothing has to be closed
or opened again.

When the app cannot have the database to itself (something keeps it busy for
a minute) the move is left for the next start to finish, as it was before this
could be done while the app ran.
"""

import logging
import os
from pathlib import Path
import shutil
from threading import Event, Lock, Thread
from typing import Any, Dict, Optional, Tuple

from ..context import ctx
from ..relocation import (
    DATABASE,
    IN_USE,
    LATE,
    MOVE_LOG,
    REFRESH_AT_START,
    SKIPPED,
    STAMP,
    MoveStopped,
    allocation_unit,
    begin_move,
    choose_existing,
    choose_target,
    cleanup_done,
    copy_done,
    copy_late,
    copy_tree,
    drive_roots,
    failure_shown,
    find_libraries,
    finish_database,
    forget_move,
    holds_library,
    measure,
    point_at,
    precopy_database,
    read_marker,
    remove_old_copy,
    write_marker,
)
from ..utils.file_tools import format_size, writable_folder
from ..utils.notify import desktop_notify

logger = logging.getLogger(__name__)

# Room wanted on the new drive beyond the library itself, for the downloads
# that follow: a drive filled to the last byte by the move helps nobody.
SPARE = 1024 * 1024 * 1024

# With less than this free on the library's drive the app says so everywhere:
# the database and the app's own files need room too, and at 0 MB the app stops
# answering altogether. Early enough that a move can still be made.
LOW_SPACE = 5 * 1024 * 1024 * 1024
SPACE_CHECK_EVERY = 300.0  # seconds between looks at the free space

# How long the move waits for the database to be free of other work before it
# leaves the switch to the next start.
GATE_TIMEOUT = 60.0

_ACTIVE = ("pausing", "measuring", "copying", "finishing", "switching")


def _note(folder: Path, text: str) -> None:
    from ..relocation import _note as note

    note(folder, text)


def _same_folder(one: Path, other: Path) -> bool:
    return os.path.normcase(os.path.abspath(one)) == os.path.normcase(os.path.abspath(other))


def _nothing(files: int, size: int) -> None:
    pass


class LibraryMoveService:
    def __init__(self) -> None:
        self._lock = Lock()
        self._stop = Event()
        # idle, pausing, measuring, copying, finishing (the last of the copy),
        # switching (the database held for a moment), copied (left for the next
        # start to finish), failed, chosen (a library already in another folder is
        # used from the next start)
        self._state = "idle"
        self._step: Optional[str] = None  # what finishing or switching is doing now
        self.live = True  # finish the move while the app runs, when it can
        self._marker_before: Dict[str, Any] = {}  # what the marker said before "chosen"
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
        self._closing = Event()
        self._warned_low = False  # a notification was shown for the drive being low now

    def close(self) -> None:
        self._stop.set()
        self._closing.set()

    def check_space(self) -> bool:
        """Whether the library's drive is low; a desktop notification the first time it is."""
        folder = ctx.config.app.app_dir
        try:
            free = shutil.disk_usage(folder).free
        except OSError:
            return False
        low = free < LOW_SPACE
        if low and not self._warned_low:
            logger.warning(f"Only {format_size(free)} free on the drive holding {folder}")
            desktop_notify(
                "Lightnovel Crawler: the drive is nearly full",
                f"Only {format_size(free)} is free where the library is ({folder}). "
                "Open Tools, Library and drives to move it to a bigger drive.",
            )
        self._warned_low = low
        return low

    def _watch_space(self) -> None:
        while not self._closing.wait(SPACE_CHECK_EVERY):
            try:
                self.check_space()
            except Exception as error:
                logger.debug(f"Could not check the free space: {error}")

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
                "low": free is not None and free < LOW_SPACE,
                "state": self._state,
                "target": str(self._target) if self._target else None,
                "files": self._files,
                "bytes": self._bytes,
                "total_files": self._total_files,
                "total_bytes": self._total_bytes,
                "error": self._error,
                "problem": self._problem,
                "step": self._step,
                "old_copy": dict(self._old) or None,
                "left_behind": self._left_behind,
            }

    def start(self, folder: str) -> Dict[str, Any]:
        """Start moving the library to ``folder``. ValueError or OSError says why not."""
        current = ctx.config.app.app_dir
        target = choose_target(current, Path(folder).expanduser())
        writable_folder(str(target))  # made now; refused now if it cannot be written to
        with self._lock:
            self._check_free()
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

    def places(self) -> Dict[str, Any]:
        """The drives the library could go to, and libraries already on them, to pick from."""
        current = ctx.config.app.app_dir
        here = os.path.splitdrive(os.path.abspath(current))[0].lower()
        roots = drive_roots()
        drives = []
        for root in roots:
            try:
                usage = shutil.disk_usage(root)
            except OSError:
                continue  # an empty card reader, a drive that has gone to sleep for good
            letter = os.path.splitdrive(str(root))[0].lower()
            drives.append(
                {
                    "path": str(root),
                    "free": usage.free,
                    "total": usage.total,
                    "current": bool(here) and letter == here,
                }
            )
        libraries = []
        for folder in find_libraries(roots, current):
            try:
                used = (folder / DATABASE).stat().st_mtime
            except OSError:
                used = None
            libraries.append({"path": str(folder), "last_used": used})
        try:
            empty = not any((current / "novels").iterdir())
        except OSError:
            empty = True
        return {"drives": drives, "libraries": libraries, "empty": empty}

    def _check_free(self) -> None:
        """Refuse (ValueError) while a move or the removal after one is going. Lock held."""
        if self._state in _ACTIVE or self._state in ("copied", "chosen"):
            raise ValueError("A move is already under way")
        if self._old.get("state") == "removing":
            raise ValueError(
                "The old copy from the last move is still being removed; "
                "try again when that is done"
            )

    def use_existing(self, folder: str) -> Dict[str, Any]:
        """From the next start, run from the library already in ``folder``.

        Nothing is copied and neither folder is changed: the library in use now
        stays where it is. ValueError or OSError says why not.
        """
        base = ctx.config.app.base_dir
        target = choose_existing(ctx.config.app.app_dir, Path(folder).expanduser())
        with self._lock:
            self._check_free()
            before = read_marker(base)
            point_at(base, target)
            self._marker_before = before
            self._state = "chosen"
            self._target = target
            self._error = None
        logger.info(f"The next start uses the library in {target}")
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
            elif state == "chosen":
                write_marker(ctx.config.app.base_dir, **self._marker_before)
                self._state = "idle"
                self._target = None
        if state == "copied":
            forget_move(ctx.config.app.base_dir)
            self._resume_downloads()
        return self.status()

    def on_startup(self) -> None:
        """After the app has started: tidy up after the last run's move."""
        base, current = ctx.config.app.base_dir, ctx.config.app.app_dir
        self.check_space()
        Thread(target=self._watch_space, daemon=True, name="library-space").start()
        marker = read_marker(base)
        if marker.get("moving_to"):
            # a copy the last run did not finish: the library is where it was,
            # and what was copied waits there for another try
            forget_move(base)
        if current != base and (current / DATABASE).is_file() and not holds_library(current):
            # a library taken up where it was found: marked as one, so that a
            # move from some other install is never copied over it
            try:
                (current / STAMP).write_text(f"{IN_USE}\n", encoding="utf-8")
            except OSError:
                pass
        failed = marker.get("failed")
        if isinstance(failed, str) and failed:
            # the start could not finish the move: say why, once
            went_to = marker.get("failed_target")
            logger.error(f"The library could not be moved to {went_to}: {failed}")
            with self._lock:
                self._state = "failed"
                self._error = f"it could not be finished when the app started ({failed})"
                if isinstance(went_to, str) and (Path(went_to) / MOVE_LOG).is_file():
                    self._error += f"; each step is in {Path(went_to) / MOVE_LOG}"
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
            copy_done(base)  # from here a restart finishes the move, whatever happens next
            with self._lock:
                self._problem = None
            if self.live and self._switch_now(current, base, target, stop):
                return
            with self._lock:
                self._state = "copied"
                self._step = None
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
                self._step = None
            self._resume_downloads()
        except Exception as error:
            logger.exception("Moving the library failed")
            forget_move(base)
            with self._lock:
                self._state = "failed"
                self._error = str(error)
                self._problem = None
                self._step = None
            self._resume_downloads()

    # ------------------------------------------------------------------ #
    # switching over while the app runs
    # ------------------------------------------------------------------ #

    def _say(self, target: Path, text: str) -> None:
        _note(target, text)
        with self._lock:
            self._step = text

    @staticmethod
    def _own_database(current: Path) -> Optional[Path]:
        """The database file, when it is kept in the library's folder (and so moves with it)."""
        from sqlalchemy.engine import make_url

        url = make_url(ctx.config.db.url)
        if url.get_backend_name() != "sqlite" or not url.database:
            return None
        database = Path(url.database)
        if not _same_folder(database.parent, current) or not database.is_file():
            return None
        return database

    def _switch_now(self, current: Path, base: Path, target: Path, stop: Event) -> bool:
        """Finish the move and use ``target`` from now on, without a restart; whether it was.

        Not, and the move left for the next start to finish, when something
        goes wrong before the switch or the database stays busy. Asked to stop
        before the switch, it stops (`MoveStopped`).
        """
        say = lambda text: self._say(target, text)  # noqa: E731
        try:
            with self._lock:
                self._state = "finishing"
            say(f"Finishing the move from {current} to {target}, with the app running")
            say("Copying what changed while it copied")
            copy_tree(current, target, (*LATE, *SKIPPED), _nothing, stop, waiting=self._waiting)
            # a download worker has the database open in a process of its own
            if "download_workers" in ctx.__dict__:
                ctx.download_workers.stop_idle()
            database = self._own_database(current)
            digests = None
            if database is not None:
                say("Copying the database")
                digests = precopy_database(database, target / DATABASE, stop)
            left = copy_late(current, target, say)
            if stop.is_set():
                raise MoveStopped()
            refresh = [name for name in REFRESH_AT_START if (current / name).is_dir()]
            refresh += sorted({name.split("/", 1)[0] for name in left} - set(refresh))
            with self._lock:
                self._state = "switching"
            say("Switching over: the app may stop answering for a few seconds")

            def finish() -> None:
                from ..config import use_data_folder

                if database is not None and digests is not None:
                    written = finish_database(database, target / DATABASE, digests)
                    _note(target, f"The database is copied ({written} block(s) changed since)")
                (target / STAMP).write_text(
                    f"{IN_USE} since it was moved from {current}\n", encoding="utf-8"
                )
                extra: Dict[str, Any] = {}
                if refresh:
                    extra["refresh"] = refresh
                if left:
                    extra["left_behind"] = len(left)
                write_marker(
                    base, path=str(target), state="moved", cleanup=True, old=str(current), **extra
                )
                use_data_folder(target)

            if database is not None:
                if not ctx.db.move_sqlite(str(target / DATABASE), finish, GATE_TIMEOUT):
                    _note(target, "The database stayed busy; the next start finishes the move")
                    logger.warning("The database stayed busy; the next start finishes the move")
                    return False
            else:
                finish()
        except MoveStopped:
            raise
        except Exception as error:
            logger.exception("Could not switch to the new folder while the app ran")
            _note(target, f"Could not switch while the app ran ({error!r}); a restart finishes it")
            return False

        # From here the library is in ``target``.
        _note(target, f"Switched: the app runs from {target}")
        self._follow(current, target)
        try:
            # anything written to the old folder in the moments before the switch
            copy_tree(current, target, (*LATE, *SKIPPED), _nothing, only_missing=True)
        except Exception as error:
            logger.warning(f"Could not bring the last new files along: {error}")
        with self._lock:
            self._state = "idle"
            self._target = None
            self._step = None
            self._left_behind = len(left)
        self._resume_downloads()
        logger.info(f"The library is now in {target}")
        desktop_notify(
            "Lightnovel Crawler: the library has moved",
            f"It is now in {target}. The old copy in {current} is being removed.",
        )
        self._remove_old(base, current, target, self._stop, keep=tuple(refresh))
        return True

    def _follow(self, old: Path, new: Path) -> None:
        """Bring the parts of the app that keep their own path along to ``new``. Best effort."""
        from ..core.scraping import follow_data_folder
        from .logger import move_log_file

        steps = [
            ("keep the settings there", lambda: ctx.config.follow_data_folder(old)),
            ("write the log there", lambda: move_log_file(old, new)),
            ("keep what is known about sites there", follow_data_folder),
        ]
        if "crawler" in ctx.__dict__:
            steps.append(("close the idle crawlers", ctx.crawler.close))
        for what, step in steps:
            try:
                step()
            except Exception as error:
                logger.warning(f"After moving the library, could not {what}: {error}")
                _note(new, f"Could not {what}: {error!r}")
        # made again, with the new folder, the next time it is used
        ctx.__dict__.pop("http", None)

    @staticmethod
    def _size(entry: Path, stop: Event) -> int:
        if entry.is_dir():
            return measure(entry, (), _nothing, stop).bytes
        try:
            return entry.stat().st_size
        except OSError:
            return 0

    def _remove_old(
        self, base: Path, old: Path, current: Path, stop: Event, keep: Tuple[str, ...] = ()
    ) -> None:
        """Remove the old copy; what ``keep`` names is left for the next start to remove."""

        def freed(files: int, size: int) -> None:
            with self._lock:
                self._old["freed"] += size
                self._old["files"] += files

        with self._lock:
            self._old = {"state": "removing", "path": str(old), "freed": 0, "files": 0, "left": 0}
        try:
            _, left = remove_old_copy(old, current, freed, stop, keep=keep)
            if not keep:
                cleanup_done(base)
            if old != base and not keep:
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
