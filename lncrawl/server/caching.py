"""HTTP caching helpers for the pages and files the server hands to browsers.

Two things are kept here:

- ``Blob``: a body held in memory together with its gzip encoding and ETag,
  so a file or page that is served again and again is read and compressed
  once instead of on every request (gzip of the 1.5 MB dashboard bundle costs
  ~80 ms of event-loop time at level 9).
- Conditional responses: ``If-None-Match`` is answered with ``304 Not
  Modified`` so a browser revalidating its cached copy gets no body back.
"""

from dataclasses import dataclass
from functools import lru_cache
import gzip
import hashlib
import os
from pathlib import Path
import threading
from typing import Dict, Mapping, Optional, Tuple

from fastapi import Request
from fastapi.responses import FileResponse, Response

# Text types worth compressing ahead of time.
_COMPRESSIBLE: Tuple[str, ...] = (
    "text/",
    "application/javascript",
    "application/json",
    "application/manifest+json",
    "application/xml",
    "image/svg+xml",
)


@dataclass(frozen=True)
class Blob:
    body: bytes
    gzipped: Optional[bytes]
    etag: str
    media_type: str


def make_blob(body: bytes, media_type: str) -> Blob:
    gzipped: Optional[bytes] = None
    if len(body) >= 1000 and media_type.startswith(_COMPRESSIBLE):
        packed = gzip.compress(body, compresslevel=9, mtime=0)
        if len(packed) < len(body):
            gzipped = packed
    digest = hashlib.blake2b(body, digest_size=16).hexdigest()
    return Blob(body=body, gzipped=gzipped, etag=f'"{digest}"', media_type=media_type)


def etag_matches(request: Request, etag: str) -> bool:
    header = request.headers.get("if-none-match")
    if not header:
        return False
    if header.strip() == "*":
        return True
    bare = etag[2:] if etag.startswith("W/") else etag
    for tag in header.split(","):
        tag = tag.strip()
        if tag.startswith("W/"):
            tag = tag[2:]
        if tag == bare:
            return True
    return False


def blob_response(
    request: Request,
    blob: Blob,
    headers: Optional[Mapping[str, str]] = None,
) -> Response:
    """Serve ``blob``: 304 when the browser's copy is current, gzip when accepted."""
    out: Dict[str, str] = dict(headers or {})
    out["ETag"] = blob.etag
    if blob.gzipped is not None:
        out["Vary"] = "Accept-Encoding"
    if etag_matches(request, blob.etag):
        return Response(status_code=304, headers=out)
    if blob.gzipped is not None and "gzip" in request.headers.get("accept-encoding", ""):
        out["Content-Encoding"] = "gzip"
        return Response(content=blob.gzipped, media_type=blob.media_type, headers=out)
    return Response(content=blob.body, media_type=blob.media_type, headers=out)


@lru_cache(maxsize=16)
def text_blob(text: str, media_type: str) -> Blob:
    """Blob of a page built in code; the same text is encoded only once."""
    return make_blob(text.encode("utf-8"), media_type)


class FileBlobCache:
    """In-memory copies of files on disk, refreshed when a file changes.

    Meant for a small, bounded set of files (the web UI build, ~4 MB). Files
    above ``max_size`` are not held; ``get`` returns ``None`` for them.
    """

    def __init__(self, max_size: int = 8 * 1024 * 1024) -> None:
        self.max_size = max_size
        self._lock = threading.Lock()
        self._items: Dict[Path, Tuple[int, int, Blob]] = {}

    def get(self, path: Path, media_type: str) -> Optional[Blob]:
        """Blocking (stats and may read the file): call it off the event loop."""
        try:
            st = os.stat(path)
        except OSError:
            return None
        if st.st_size > self.max_size:
            return None
        with self._lock:
            hit = self._items.get(path)
        if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
            return hit[2]
        blob = make_blob(path.read_bytes(), media_type)
        with self._lock:
            self._items[path] = (st.st_mtime_ns, st.st_size, blob)
        return blob


def cached_file_response(
    request: Request,
    path: Path,
    media_type: str,
    cache_control: str = "private, no-cache",
) -> Response:
    """A ``FileResponse`` that answers a matching ``If-None-Match`` with 304.

    ``no-cache`` lets the browser keep the file but makes it check back first,
    so a changed file (a new cover, a repaired image) is picked up at once
    while an unchanged one costs a 304 with no body.
    """
    stat = os.stat(path)
    response = FileResponse(
        path,
        media_type=media_type,
        stat_result=stat,
        headers={"Cache-Control": cache_control},
    )
    etag = response.headers.get("etag", "")
    if etag and etag_matches(request, etag):
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": cache_control},
        )
    return response
