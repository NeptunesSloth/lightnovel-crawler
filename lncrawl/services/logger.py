import inspect
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
from typing import Any, Optional, Union

_FILE_HANDLER_NAME = "lncrawl-file"


def _stderr_is_interactive() -> bool:
    """Whether stderr is a real terminal we can safely draw a progress bar on.

    In a windowed/frozen build (no console) stderr is missing or backed by an
    invalid handle. Drawing a tqdm progress bar there raises
    ``OSError: [WinError 6] The handle is invalid`` on Windows, which would crash
    every chapter/image download. Only show bars on an actual TTY.
    """
    try:
        stream = getattr(sys, "stderr", None)
        return bool(stream) and stream.isatty()
    except Exception:
        return False


def _stream_is_usable(stream: Any) -> bool:
    """Whether writes to ``stream`` can reach somewhere without raising.

    Flushing alone does not answer this. A freshly detached stream still has an
    empty buffer, and flushing an empty buffer returns without ever touching the
    OS handle — so the check passes and the next real write raises anyway. Ask
    the file descriptor instead: after ``FreeConsole`` the handle behind fd 1/2
    is gone and ``os.fstat`` fails on it, which is exactly the state we need to
    catch.
    """
    if stream is None:
        return False
    try:
        fileno = stream.fileno()
    except Exception:
        # Streams without a descriptor (``pythonw``, an already-wrapped object,
        # a captured buffer) cannot be probed this way; a flush is all we have.
        fileno = None
    if fileno is not None:
        if fileno < 0:
            return False
        try:
            os.fstat(fileno)
        except Exception:
            return False
    try:
        stream.flush()
        return True
    except Exception:
        return False


def repair_std_streams() -> None:
    """Replace missing/broken std streams so writes can't raise WinError 6.

    A windowed/frozen build (the desktop ``.exe``) has no console — it is built
    for the console subsystem and then calls ``FreeConsole`` — so
    ``sys.stdout``/``sys.stderr`` are ``None`` or backed by an invalid OS handle.
    Anything that writes to them — ``tqdm.write`` (which ignores the bar's
    ``disable`` flag), the logging fallback handler, stray ``print`` — then
    raises ``OSError: [WinError 6] The handle is invalid`` and crashes downloads.
    Point any unusable stream at the null device so those writes are harmless.
    """
    devnull = None
    for name in ("stdout", "stderr"):
        if _stream_is_usable(getattr(sys, name, None)):
            continue
        if devnull is None:
            devnull = open(os.devnull, "w")
        setattr(sys, name, devnull)


def log_to_file(path: Path, level: int = logging.INFO) -> Optional[Path]:
    """Also write logs to ``path``, independently of the console log level.

    The desktop app frees its console and runs at level 0, so without this a
    failed launch leaves nothing behind at all: the window closes and there is
    no record of what went wrong. The file handler is attached to the root
    logger directly rather than through ``basicConfig`` so it survives the
    ``force=True`` reconfiguration in :meth:`Logger.setup`, and the root level is
    only ever lowered — never raised — so attaching it cannot silence a console
    handler that wanted more.

    Returns the path being written to, or ``None`` if it could not be opened
    (a read-only data dir must not take the app down with it).
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
            delay=True,
        )
    except Exception:
        return None

    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    handler.set_name(_FILE_HANDLER_NAME)

    root = logging.getLogger()
    for existing in root.handlers:
        if existing.get_name() == _FILE_HANDLER_NAME:
            return path
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)
    return path


_crash_file: Optional[Any] = None


def enable_crash_log(path: Path) -> Optional[Path]:
    """Keep a record of how the app ended when it was not by choice; where it is.

    A fatal error in native code (a crash) writes every thread's stack to
    ``path``, and an error nothing caught in any thread is logged with its
    traceback: on a machine left downloading for days, an app that closed
    left nothing to say why. None if the file cannot be opened.
    """
    global _crash_file
    import faulthandler
    import threading

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        crash_file = open(path, "a", encoding="utf-8")  # noqa: SIM115 -- kept open on purpose
    except OSError:
        return None
    crash_file.write(f"--- app started {_now()} ---\n")
    crash_file.flush()
    faulthandler.enable(crash_file, all_threads=True)
    previous, _crash_file = _crash_file, crash_file
    if previous is not None:
        try:
            previous.close()
        except Exception:
            pass

    log = logging.getLogger("lncrawl.crash")

    def thread_error(args: Any) -> None:
        if args.exc_type is SystemExit:
            return
        name = getattr(args.thread, "name", "a thread")
        log.critical(
            f"Uncaught error in {name}",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    def main_error(kind: Any, value: Any, traceback: Any) -> None:
        if not issubclass(kind, KeyboardInterrupt):
            log.critical("Uncaught error", exc_info=(kind, value, traceback))
        sys.__excepthook__(kind, value, traceback)

    threading.excepthook = thread_error
    sys.excepthook = main_error
    return path


def _now() -> str:
    import time

    return time.strftime("%Y-%m-%d %H:%M:%S")


def move_log_file(old: Path, new: Path) -> Optional[Path]:
    """Write the log file kept in folder ``old`` in folder ``new`` from now on; where it is now.

    For a library moved while the app runs: the log was in the old folder,
    whose copy is then removed. None when no log file is kept in ``old``.
    """
    root = logging.getLogger()
    for existing in list(root.handlers):
        if existing.get_name() != _FILE_HANDLER_NAME:
            continue
        path = Path(getattr(existing, "baseFilename", ""))
        try:
            relative = path.relative_to(old)
        except ValueError:
            return None
        level = existing.level
        root.removeHandler(existing)
        existing.close()
        if _crash_file is not None:
            enable_crash_log(new / relative.parent / "crash.log")
        return log_to_file(new / relative, level)
    return None


class Logger:
    def __init__(self) -> None:
        self._level = logging.NOTSET
        self.progress_bar: bool = False

    @property
    def level(self) -> int:
        return self._level

    @property
    def is_debug(self) -> bool:
        return self.level == logging.DEBUG

    @property
    def is_info(self) -> bool:
        return self.is_debug or self.level == logging.INFO

    @property
    def is_warn(self) -> bool:
        return self.is_info or self.level == logging.WARN

    @property
    def has_exception(self) -> bool:
        try:
            return sys.exc_info()[1] is not None
        except Exception:
            return False

    def setup(self, level: Union[int, str, None] = None) -> None:
        repair_std_streams()
        if level is None:
            level = int(os.getenv("LNCRAWL_LOG_LEVEL", "0"))
        if isinstance(level, int):
            levels = ["NOTSET", "WARN", "INFO", "DEBUG"]
            level = levels[max(0, min(level, 3))]
        if level:
            self._level = getattr(logging, level, logging.NOTSET)

        if self._level > 0:
            from rich.logging import RichHandler

            handler = RichHandler(
                level=self._level,
                tracebacks_show_locals=False,
                rich_tracebacks=True,
                markup=True,
            )
            logging.basicConfig(
                level=self._level,
                handlers=[handler],
                format="%(message)s",
                datefmt="[%X]",
                force=True,
            )
        self.progress_bar = (not self.is_info) and _stderr_is_interactive()

    def log(self, level: int, *args, **kwargs) -> None:
        stacklevel = kwargs.pop("stacklevel", 0) + 2
        frame = inspect.stack()[stacklevel]
        kwargs["stacklevel"] = stacklevel
        logger = logging.getLogger(frame.filename)
        logger.log(level, *args, **kwargs)

    def error(self, *args, **kwargs) -> None:
        self.log(logging.ERROR, *args, **kwargs, stacklevel=1)

    def warn(self, *args, **kwargs) -> None:
        self.log(logging.WARNING, *args, **kwargs, stacklevel=1)

    def info(self, *args, **kwargs) -> None:
        self.log(logging.INFO, *args, **kwargs, stacklevel=1)

    def debug(self, *args, **kwargs) -> None:
        self.log(logging.DEBUG, *args, **kwargs, stacklevel=1)
