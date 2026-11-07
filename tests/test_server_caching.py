"""Response compression and HTTP caching of the pages and files the server serves."""

import gzip
import os
from pathlib import Path
import tempfile
from typing import Iterator
import unittest

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from fastapi.testclient import TestClient

from lncrawl.server.caching import (
    FileBlobCache,
    blob_response,
    cached_file_response,
    etag_matches,
    text_blob,
)
from lncrawl.server.middleware.compression import SelectiveGZipMiddleware

TEXT = "Words. " * 2000
BINARY = os.urandom(5000)


def _app(folder: Path) -> FastAPI:
    app = FastAPI()
    app.add_middleware(SelectiveGZipMiddleware, minimum_size=1000)
    (folder / "book.epub").write_bytes(BINARY)
    (folder / "big.txt").write_text(TEXT * 20)

    @app.get("/text")
    def text() -> Response:
        return PlainTextResponse(TEXT)

    @app.get("/small")
    def small() -> Response:
        return PlainTextResponse("tiny")

    @app.get("/jpeg")
    def jpeg() -> Response:
        return Response(BINARY, media_type="image/jpeg")

    @app.get("/svg")
    def svg() -> Response:
        return Response(f"<svg>{TEXT}</svg>", media_type="image/svg+xml")

    @app.get("/epub")
    def epub() -> Response:
        return FileResponse(folder / "book.epub", media_type="application/epub+zip")

    @app.get("/big.txt")
    def big() -> Response:
        return FileResponse(folder / "big.txt", media_type="text/plain")

    @app.get("/stream")
    def stream() -> Response:
        def chunks() -> Iterator[str]:
            for _ in range(5):
                yield TEXT

        return StreamingResponse(chunks(), media_type="text/plain")

    @app.get("/events")
    def events() -> Response:
        return StreamingResponse(iter([TEXT]), media_type="text/event-stream")

    @app.get("/page")
    def page(request: Request) -> Response:
        return blob_response(request, text_blob(TEXT, "text/html"), {"Cache-Control": "no-cache"})

    @app.get("/cover")
    def cover(request: Request) -> Response:
        return cached_file_response(request, folder / "book.epub", "image/jpeg")

    return app


class SelectiveGZipTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = TestClient(_app(Path(self.tmp.name)))
        self.addCleanup(self.client.close)
        self.gz = {"Accept-Encoding": "gzip"}

    def test_text_is_compressed(self) -> None:
        res = self.client.get("/text", headers=self.gz)
        self.assertEqual(res.headers.get("content-encoding"), "gzip")
        self.assertIn("accept-encoding", res.headers.get("vary", "").lower())
        self.assertEqual(res.text, TEXT)
        self.assertLess(int(res.headers["content-length"]), len(TEXT))

    def test_small_or_unasked_is_left_alone(self) -> None:
        res = self.client.get("/small", headers=self.gz)
        self.assertNotIn("content-encoding", res.headers)
        res = self.client.get("/text", headers={"Accept-Encoding": "identity"})
        self.assertNotIn("content-encoding", res.headers)
        self.assertEqual(res.text, TEXT)

    def test_binary_downloads_keep_their_length(self) -> None:
        for path in ("/jpeg", "/epub"):
            res = self.client.get(path, headers=self.gz)
            self.assertNotIn("content-encoding", res.headers, path)
            self.assertEqual(res.headers["content-length"], str(len(BINARY)), path)
            self.assertEqual(res.content, BINARY, path)

    def test_svg_is_text(self) -> None:
        res = self.client.get("/svg", headers=self.gz)
        self.assertEqual(res.headers.get("content-encoding"), "gzip")

    def test_byte_ranges_are_not_reencoded(self) -> None:
        res = self.client.get("/big.txt", headers={**self.gz, "Range": "bytes=0-99"})
        self.assertEqual(res.status_code, 206)
        self.assertNotIn("content-encoding", res.headers)
        self.assertEqual(len(res.content), 100)

    def test_streams_and_files_are_compressed_whole(self) -> None:
        res = self.client.get("/stream", headers=self.gz)
        self.assertEqual(res.headers.get("content-encoding"), "gzip")
        self.assertEqual(res.text, TEXT * 5)
        res = self.client.get("/big.txt", headers=self.gz)
        self.assertEqual(res.headers.get("content-encoding"), "gzip")
        self.assertEqual(res.text, TEXT * 20)
        self.assertTrue(res.headers["etag"].startswith("W/"))

    def test_event_streams_are_not_buffered(self) -> None:
        res = self.client.get("/events", headers=self.gz)
        self.assertNotIn("content-encoding", res.headers)

    def test_pages_revalidate_with_304(self) -> None:
        first = self.client.get("/page", headers=self.gz)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.headers.get("content-encoding"), "gzip")
        self.assertEqual(first.text, TEXT)
        etag = first.headers["etag"]
        again = self.client.get("/page", headers={**self.gz, "If-None-Match": etag})
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.content, b"")
        self.assertEqual(again.headers["etag"], etag)
        other = self.client.get("/page", headers={"If-None-Match": '"nope"'})
        self.assertEqual(other.status_code, 200)
        self.assertEqual(other.text, TEXT)

    def test_files_revalidate_with_304(self) -> None:
        first = self.client.get("/cover", headers=self.gz)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.content, BINARY)
        self.assertEqual(first.headers["cache-control"], "private, no-cache")
        again = self.client.get("/cover", headers={"If-None-Match": first.headers["etag"]})
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.content, b"")


class BlobCacheTest(unittest.TestCase):
    def test_reloads_a_changed_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.js"
            path.write_text("let a = 1;" * 200)
            cache = FileBlobCache()
            first = cache.get(path, "application/javascript")
            assert first is not None
            self.assertIs(cache.get(path, "application/javascript"), first)
            self.assertIsNotNone(first.gzipped)
            assert first.gzipped is not None
            self.assertEqual(gzip.decompress(first.gzipped), path.read_bytes())
            path.write_text("let b = 2;" * 300)
            second = cache.get(path, "application/javascript")
            assert second is not None
            self.assertEqual(second.body, path.read_bytes())
            self.assertNotEqual(first.etag, second.etag)
            self.assertIsNone(cache.get(Path(tmp) / "missing.js", "application/javascript"))

    def test_weak_and_listed_etags_match(self) -> None:
        class _Req:
            def __init__(self, value: str) -> None:
                self.headers = {"if-none-match": value}

        etag = '"abc"'
        self.assertTrue(etag_matches(_Req('W/"abc"'), etag))  # type: ignore[arg-type]
        self.assertTrue(etag_matches(_Req('"x", "abc"'), etag))  # type: ignore[arg-type]
        self.assertFalse(etag_matches(_Req('"x"'), etag))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
