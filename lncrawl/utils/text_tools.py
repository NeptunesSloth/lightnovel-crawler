import html
import re
import threading
import unicodedata
import uuid

import zstd

_RE_SPACES = re.compile(r"\s+")
# zstd 1.5.7.3 (the only release with wheels for Python 3.14) compresses with one
# context for the whole process and lets go of the GIL while it does: two
# chapters saved at once by two download threads could corrupt each other or
# crash the app (python-zstd issues #321, #323; the release is yanked for it).
# A 50 KB chapter takes a quarter of a millisecond, so taking turns costs nothing.
_ZSTD_LOCK = threading.Lock()


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKD", text).casefold()


def format_title(text: str) -> str:
    name = html.unescape(str(text or ""))
    name = unicodedata.normalize("NFKC", name)
    return _RE_SPACES.sub(" ", name).strip().title()


def is_compressed(data: bytes) -> bool:
    return data.startswith(b"\x28\xb5\x2f\xfd")


def text_compress(plain: bytes) -> bytes:
    with _ZSTD_LOCK:
        return zstd.compress(plain, 10)


def text_decompress(compressed: bytes) -> bytes:
    with _ZSTD_LOCK:
        return zstd.decompress(compressed)


def generate_uuid() -> str:
    return str(uuid.uuid4())
