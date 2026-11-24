from contextlib import contextmanager
import logging
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Iterator, Union

from slugify import slugify

from .platforms import Platform

logger = logging.getLogger(__name__)


@contextmanager
def atomic_write(path: Union[str, Path], mode: str = "wb") -> Iterator:
    """Atomically write to `path` via a sibling temp file in the same directory.

    On context exit the temp file is renamed onto the target with `os.replace`,
    which is atomic on POSIX and Windows. On exception the temp file is removed
    and the original (if any) is left untouched.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, mode) as f:
            yield f
            # On the drive before it takes the file's name: a cable pulled just
            # after the rename could leave the name on an empty or cut file,
            # which a page image or a state file was then taken for whole.
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        _replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# Windows refuses to replace a file another program has open for a moment (an
# antivirus scan, the search indexer, a reader) or one marked read-only: tried
# again this many times, a little longer each time.
REPLACE_TRIES = 5
REPLACE_WAIT = 0.2
_WINDOWS = os.name == "nt"


def _replace(source: str, path: Path) -> None:
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(source, path)
            return
        except PermissionError:
            if not _WINDOWS or attempt + 1 >= REPLACE_TRIES:
                raise
            try:
                os.chmod(path, stat.S_IREAD | stat.S_IWRITE)  # read-only: allow it
            except OSError:
                pass
            time.sleep(REPLACE_WAIT * (attempt + 1))


def move_into_place(made: Union[str, Path], target: Union[str, Path]) -> None:
    """Put a finished file at ``target``, replacing what is there only once it has arrived.

    The e-book makers deleted the old book and then renamed the new one over
    from the work folder. With the work folder on one drive and the novel on
    another, Windows refuses that rename, and the old book was already gone.
    The new file is brought beside the target first, then swapped in.
    """
    made, target = Path(made), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    arriving = target.with_name(f".{target.name}.arriving")
    try:
        shutil.move(str(made), str(arriving))  # a copy when it is another drive
        _replace(str(arriving), target)
    except BaseException:
        try:
            arriving.unlink()
        except OSError:
            pass
        raise


def format_size(num_bytes: int, decimals: int = 1, suffix: str = "B") -> str:
    """
    Convert a size in bytes into a human-readable string representing the size,
    e.g. "117.7 MB" or "9.0 GB".

    Units go up to zettabytes (ZB), then yottabytes (YB) as a fallback.
    """
    units = ["", "K", "M", "G", "T", "P", "E", "Z"]
    size = float(num_bytes)
    for unit in units:
        if abs(size) < 1024.0:
            return f"{size:.{decimals}f} {unit}{suffix}"
        size /= 1024.0
    return f"{size:.{decimals}f} Y{suffix}"


def dir_size(folder: Union[str, Path], *, allocated: bool = False) -> int:
    """Total size, in bytes, of everything under *folder*.

    Walks with :func:`os.scandir` rather than ``Path.rglob``: the directory entry
    already carries the type and the stat, so this is one syscall per file instead
    of three. Measured over 40,000 files that is 111ms against 554ms, which
    matters because the storage view sizes every novel separately.

    ``allocated`` picks which question is being asked. False sums apparent sizes —
    how many bytes of content there are. True sums whole allocated blocks, for
    directories as well as files, which is the disk the library actually occupies
    and matches ``du -s -B1`` exactly. The two differ by more than rounding on a
    library of small chapter files.

    ``st_blocks`` is POSIX-only; where it is missing (Windows) ``allocated`` falls
    back to apparent size, which is what the shell commands this replaced also
    reported there. Unreadable entries are skipped.
    """
    total = 0
    stack = [str(folder)]
    while stack:
        current = stack.pop()
        if allocated:
            try:
                total += _entry_bytes(os.stat(current), allocated)
            except OSError:
                pass
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += _entry_bytes(entry.stat(follow_symlinks=False), allocated)
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _entry_bytes(stat_result: os.stat_result, allocated: bool) -> int:
    if not allocated:
        return stat_result.st_size
    blocks = getattr(stat_result, "st_blocks", None)
    return stat_result.st_size if blocks is None else blocks * 512


def folder_size(folder: Union[str, Path]) -> int:
    """Disk occupied by *folder*, in bytes. 0 if it cannot be read.

    The number the scrubber decides by, so it counts allocated blocks rather than
    apparent size — the same measure :func:`dir_size` reports with
    ``allocated=True``, and the same one the storage view shows.

    Still shells out, because for a single large tree ``du`` beats the Python walk
    by about 5x (24ms against 115ms over 6,000 files) — it is C, and this is
    called on the whole data directory. The walk is the fallback, and is used
    directly where many small folders are sized one by one, since a subprocess
    each would cost far more than it saves.
    """
    try:
        if Platform.windows:
            cmd = [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-ChildItem -LiteralPath '{folder}' -Recurse -Force -File | Measure-Object Length -Sum).Sum",
            ]
            out = subprocess.check_output(cmd, text=True).strip()
            return int(out) if out else 0
        elif Platform.posix:
            cmd = shlex.split(f'du -s -B1 "{folder}"')
            out = subprocess.check_output(cmd, text=True).strip()
            return int(out.split()[0]) if out else 0
    except Exception:
        logger.warning("System command failed, falling back to Python scan", exc_info=True)

    # Fallback. Counts allocated blocks too, so it agrees with the command above
    # instead of quietly reporting a much smaller apparent total.
    try:
        return dir_size(folder, allocated=True)
    except Exception:
        logger.error("Failed to calculate folder size in Python fallback", exc_info=True)
        return 0


# Windows reserved device names
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_filename(name: str) -> str:
    name = (
        slugify(
            name,
            max_length=255,
            separator=" ",
            # Windows forbidden: < > : " / \ | ? *
            # URL reserved/special: # % & ; = @ ^ ` { } !
            # Control chars: \x00-\x1F (C0) and \x7F (DEL)
            regex_pattern=r'[<>:"/\\|?*#%&+;=@^`{}!\x00-\x1F\x7F]',
        )
        .strip(" .")
        .strip("-")
        or "untitled"
    )
    if name.upper() in _WINDOWS_RESERVED:
        name = f"_{name}"
    return name or "untitled"


def writable_folder(folder: str) -> Path:
    """``folder`` as a path that exists and can be written to now.

    Made if missing. Raises ValueError for a relative path (relative to what the
    server happens to run in is never what was meant) and OSError, with the
    reason, when the folder cannot be made or written to.
    """
    path = Path(folder).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{folder} is not a full path to a folder")
    path.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path, prefix=".lncrawl-write-check-"):
        pass
    return path


def open_folder(folder_path: Union[str, Path]) -> None:
    if Platform.windows:
        os.system(f'explorer.exe "{folder_path}"')
    elif Platform.wsl:
        os.system(f'cd "{folder_path}" && explorer.exe .')
    elif Platform.linux:
        os.system(f'xdg-open "{folder_path}"')
    elif Platform.mac:
        os.system(f'open "{folder_path}"')
