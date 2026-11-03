"""Keep the computer from going to sleep while a long download runs.

A bulk download left alone for days is only as good as the machine staying
awake: a laptop that sleeps after 30 minutes stops it cold, and a job that
says "running" while nothing happens is the worst way for it to fail. While
one of these is held, the operating system is asked not to sleep for idleness.
The screen may still turn off. Best-effort: where it cannot be done (a
headless server, no tool for it) it quietly does nothing.

- Windows: ``SetThreadExecutionState`` from the thread running the download.
- macOS: ``caffeinate -i`` tied to this process.
- Linux: ``systemd-inhibit`` holding an idle/sleep lock while it runs.
"""

from contextlib import contextmanager
import logging
import os
import platform
import shutil
import subprocess
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


def _start() -> Optional[subprocess.Popen]:
    system = platform.system()
    if system == "Windows":
        import ctypes

        kernel = getattr(ctypes, "windll").kernel32
        kernel.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)
        return None
    if system == "Darwin" and shutil.which("caffeinate"):
        return subprocess.Popen(
            ["caffeinate", "-i", "-w", str(os.getpid())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    if system == "Linux" and shutil.which("systemd-inhibit") and shutil.which("sleep"):
        return subprocess.Popen(
            [
                "systemd-inhibit",
                "--what=idle:sleep",
                "--who=Lightnovel Crawler",
                "--why=Downloading novels",
                "--mode=block",
                "sleep",
                "infinity",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return None


def _stop(process: Optional[subprocess.Popen]) -> None:
    if platform.system() == "Windows":
        import ctypes

        getattr(ctypes, "windll").kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
    if process is not None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except Exception:
            process.kill()


@contextmanager
def keep_awake(enabled: bool = True) -> Iterator[None]:
    """Hold the computer awake for the duration of the ``with`` block."""
    process: Optional[subprocess.Popen] = None
    held = False
    if enabled:
        try:
            process = _start()
            held = True
        except Exception as error:
            logger.debug(f"Could not keep the computer awake: {error}")
    try:
        yield
    finally:
        if held:
            try:
                _stop(process)
            except Exception as error:
                logger.debug(f"Could not release the keep-awake hold: {error}")
