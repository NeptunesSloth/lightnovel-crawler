from functools import lru_cache
import json
import logging
import os
from pathlib import Path
import stat
from threading import Lock
from typing import Dict, Iterable, List, Set, Tuple, Union

from zstd import Error as ZstdError

from ..context import ctx
from ..exceptions import ServerError, ServerErrors
from ..utils.chapter_content import has_chapter_content
from ..utils.file_tools import atomic_write
from ..utils.text_tools import is_compressed, text_compress, text_decompress

logger = logging.getLogger(__name__)

StrPath = Union[str, Path]

# Kept in each novel's chapters folder: the chapter files already read and found
# to hold a chapter, by size and time. A file still the same is not read again.
CHECKED_FILE = ".checked.json"
_checked_lock = Lock()


@lru_cache(maxsize=4096)
def _chapter_content_available(file: Path, signature: Tuple[int, ...]) -> bool:
    # Cache only the verdict, not chapter text. The stat signature changes on
    # writes/replacements, including edits made outside this process.
    data = file.read_bytes()
    try:
        if is_compressed(data):
            data = text_decompress(data)
        return has_chapter_content(data.decode("utf-8"))
    except (UnicodeError, ZstdError):
        return False


class FileService:
    def __init__(self) -> None:
        pass

    @property
    def root(self):
        return ctx.config.app.app_dir

    def resolve(self, file: StrPath) -> Path:
        if isinstance(file, str):
            file = Path(file.lstrip("/"))
        if file.is_absolute():
            return file
        books = getattr(ctx, "books", None)  # a novel's books kept on a drive
        on_drive = books.route(file) if books is not None else None
        return on_drive if on_drive is not None else self.root / file

    def _check_drive(self, file: Path) -> None:
        """Raise BookDriveUnplugged when ``file`` is on the book drive and it is not there."""
        books = getattr(ctx, "books", None)
        folder = books.folder() if books is not None else None
        if books is None or folder is None:
            return
        try:
            file.relative_to(folder)
        except ValueError:
            return
        if not books.plugged():
            from .books import BookDriveUnplugged

            raise BookDriveUnplugged(f"The drive with these books ({folder}) is not plugged in")

    def exists(self, file_path: StrPath):
        return self.resolve(file_path).is_file()

    def chapters_with_content(self, novel_id: str, serials: Iterable[int]) -> Set[int]:
        """Which of these chapters of a novel have a saved file holding a chapter.

        The answer for each file is kept beside them (`CHECKED_FILE`), and a file
        whose size and time have not changed since is taken at its word. Every
        file was read on every run before: 4,932 for one long novel, from an
        external drive, before its first missing chapter was asked for, and four
        novels at a time. The folder's listing gives the sizes and times, so a
        file that is the same costs nothing; a new or changed one is read.
        """
        folder = self.resolve(f"novels/{novel_id}/chapters")
        wanted = {f"{serial:06}.zst": serial for serial in serials}
        if not wanted:
            return set()
        listed: Dict[str, List[int]] = {}
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.name in wanted:
                        try:
                            info = entry.stat()
                        except OSError:
                            continue
                        if stat.S_ISREG(info.st_mode):
                            listed[entry.name] = [info.st_size, info.st_mtime_ns]
        except FileNotFoundError:
            return set()
        index = folder / CHECKED_FILE
        try:
            checked = json.loads(index.read_text(encoding="utf-8")).get("files", {})
            if not isinstance(checked, dict):
                checked = {}
        except (OSError, ValueError, AttributeError):
            checked = {}
        found: Set[int] = set()
        changed = False
        for name, signature in listed.items():
            if checked.get(name) == signature:
                found.add(wanted[name])
                continue
            if self.has_chapter_content(folder / name):
                checked[name] = signature
                found.add(wanted[name])
                changed = True
            elif name in checked:
                del checked[name]
                changed = True
        if changed:
            with _checked_lock:
                try:
                    with atomic_write(index) as temporary:
                        temporary.write(json.dumps({"files": checked}).encode("utf-8"))
                except OSError as error:
                    logger.debug(f"Could not keep the chapter checks in {index}: {error}")
        return found

    def stored_images(self, novel_id: str) -> Set[str]:
        """The ids of a novel's page images that are stored (and not empty), in one listing."""
        folder = self.resolve(f"novels/{novel_id}/images")
        found: Set[str] = set()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    if not entry.name.endswith(".jpg"):
                        continue
                    try:
                        if entry.stat().st_size > 0:
                            found.add(entry.name[:-4])
                    except OSError:
                        continue
        except FileNotFoundError:
            return set()
        return found

    def has_chapter_content(self, file_path: StrPath) -> bool:
        file = self.resolve(file_path)
        try:
            info = file.stat()
            if not stat.S_ISREG(info.st_mode):
                return False
            return _chapter_content_available(
                file,
                (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns),
            )
        except FileNotFoundError:
            return False

    def load(self, file_path: StrPath) -> bytes:
        file = self.resolve(file_path)
        if not file.is_file():
            try:
                self._check_drive(file)
            except OSError as error:  # said as it is to whoever was reading
                raise ServerError(503, str(error)) from error
            raise ServerErrors.no_such_file
        return file.read_bytes()

    def save(self, file_path: StrPath, content: bytes) -> Path:
        file = self.resolve(file_path)
        self._check_drive(file)
        file.parent.mkdir(parents=True, exist_ok=True)
        with atomic_write(file) as temporary:
            temporary.write(content)
        return file

    def load_text(
        self,
        file_path: StrPath,
        encoding: str = "utf-8",
    ) -> str:
        data = self.load(file_path)
        if is_compressed(data):
            data = text_decompress(data)
        return data.decode(encoding)

    def save_text(
        self,
        file_path: StrPath,
        content: str,
        compress: bool = True,
        encoding: str = "utf-8",
    ) -> Path:
        data = content.encode(encoding)
        if compress or is_compressed(data):
            data = text_compress(data)
        return self.save(file_path, data)

    def utime(self, file: StrPath) -> None:
        path = self.resolve(file)
        if path.exists():
            os.utime(path)
