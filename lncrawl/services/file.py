from functools import lru_cache
import logging
import os
from pathlib import Path
import stat
from typing import Tuple, Union

from ..context import ctx
from ..exceptions import ServerErrors
from ..utils.chapter_content import has_chapter_content
from ..utils.file_tools import atomic_write
from ..utils.text_tools import is_compressed, text_compress, text_decompress

logger = logging.getLogger(__name__)

StrPath = Union[str, Path]


@lru_cache(maxsize=4096)
def _chapter_content_available(file: Path, signature: Tuple[int, ...]) -> bool:
    # Cache only the verdict, not chapter text. The stat signature changes on
    # writes/replacements, including edits made outside this process.
    data = file.read_bytes()
    if is_compressed(data):
        data = text_decompress(data)
    return has_chapter_content(data.decode("utf-8"))


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
        return self.root / file

    def exists(self, file_path: StrPath):
        return self.resolve(file_path).is_file()

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
            raise ServerErrors.no_such_file
        return file.read_bytes()

    def save(self, file_path: StrPath, content: bytes) -> Path:
        file = self.resolve(file_path)
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
