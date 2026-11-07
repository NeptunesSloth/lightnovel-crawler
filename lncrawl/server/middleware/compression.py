import gzip
import io
from typing import Optional

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Bodies that are already compressed (or are binary downloads) gain nothing
# from gzip: compressing them only burns CPU on the event loop and, because
# the compressed length is unknown up front, strips Content-Length, so the
# browser can show no progress or size for a large e-book or zip download.
_SKIP_TYPES = (
    "text/event-stream",
    "image/",
    "audio/",
    "video/",
    "font/woff",
    "application/zip",
    "application/x-zip",
    "application/gzip",
    "application/x-gzip",
    "application/x-7z",
    "application/x-rar",
    "application/x-bzip",
    "application/x-xz",
    "application/epub",
    "application/pdf",
    "application/x-mobipocket",
    "application/vnd.amazon",
    "application/vnd.openxmlformats",
    "application/octet-stream",
)


def _compressible(message: Message) -> bool:
    if message.get("status", 200) in (204, 206, 304):
        return False  # no body, or a byte range of the original body
    headers = Headers(raw=message["headers"])
    if "content-encoding" in headers or "content-range" in headers:
        return False
    content_type = headers.get("content-type", "").lower()
    if content_type.startswith("image/svg"):
        return True  # SVG is text and compresses well
    return not content_type.startswith(_SKIP_TYPES)


class SelectiveGZipMiddleware:
    """GZip text responses only; binary and already-compressed ones pass through.

    Starlette's GZipMiddleware compresses every content type (zip, epub, jpeg)
    at level 9. This compresses text at level 6 (within ~0.3% of the size of
    level 9, about a quarter less CPU) and leaves the rest untouched.
    """

    def __init__(self, app: ASGIApp, minimum_size: int = 1000, compresslevel: int = 6) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or "gzip" not in Headers(scope=scope).get("accept-encoding", ""):
            await self.app(scope, receive, send)
            return
        await _GZipResponder(self.app, self.minimum_size, self.compresslevel)(scope, receive, send)


class _GZipResponder:
    def __init__(self, app: ASGIApp, minimum_size: int, compresslevel: int) -> None:
        self.app = app
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel
        self.send: Optional[Send] = None
        self.start: Optional[Message] = None
        self.started = False
        self.passthrough = False
        self.buffer = io.BytesIO()
        self.gzip: Optional[gzip.GzipFile] = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.send = send
        try:
            await self.app(scope, receive, self._send)
        finally:
            if self.gzip is not None:
                self.gzip.close()
            self.buffer.close()

    def _compress(self, body: bytes, more_body: bool) -> bytes:
        assert self.gzip is not None
        self.gzip.write(body)
        if more_body:
            self.gzip.flush()
        else:
            self.gzip.close()
        out = self.buffer.getvalue()
        self.buffer.seek(0)
        self.buffer.truncate()
        return out

    async def _send(self, message: Message) -> None:
        assert self.send is not None
        kind = message["type"]
        if kind == "http.response.start":
            # Hold the headers until the first body chunk says what to do.
            self.start = message
            self.passthrough = not _compressible(message)
            return
        if kind != "http.response.body":
            if not self.started and self.start is not None:
                self.started = True
                await self.send(self.start)
            await self.send(message)
            return

        body: bytes = message.get("body", b"")
        more_body: bool = message.get("more_body", False)
        if not self.started:
            self.started = True
            assert self.start is not None
            if self.passthrough or (not more_body and len(body) < self.minimum_size):
                await self.send(self.start)
                await self.send(message)
                self.passthrough = True
                return
            self.gzip = gzip.GzipFile(
                mode="wb", fileobj=self.buffer, compresslevel=self.compresslevel
            )
            headers = MutableHeaders(raw=self.start["headers"])
            headers.add_vary_header("Accept-Encoding")
            headers["Content-Encoding"] = "gzip"
            if "content-length" in headers:
                del headers["Content-Length"]
            # A strong validator no longer matches the re-encoded bytes.
            etag = headers.get("etag")
            if etag and not etag.startswith("W/"):
                headers["ETag"] = f"W/{etag}"
            body = self._compress(body, more_body)
            if not more_body:
                headers["Content-Length"] = str(len(body))
            await self.send(self.start)
            await self.send({"type": "http.response.body", "body": body, "more_body": more_body})
            return

        if self.passthrough:
            await self.send(message)
            return
        body = self._compress(body, more_body)
        await self.send({"type": "http.response.body", "body": body, "more_body": more_body})
