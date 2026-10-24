from concurrent.futures import Future
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from lncrawl.exceptions import AbortedException
from lncrawl.services.crawl_engine import CrawlStalledError, crawl_items
from lncrawl.services.scheduler.handlers._base import HandlerException
from lncrawl.services.scheduler.handlers.export_source import (
    _build_index_html,
    _restore_archived_books,
    _save_manifest,
)


class ExportWatchdogTests(unittest.TestCase):
    def test_hung_download_stops_instead_of_replacing_worker_pools(self) -> None:
        future = Future()
        signal = Event()
        with self.assertRaisesRegex(CrawlStalledError, "No crawl worker completed"):
            list(crawl_items(["chapter"], lambda _: future, signal, idle_timeout=0.02))
        self.assertFalse(signal.is_set())
        self.assertTrue(future.cancelled())

    def test_stop_is_checked_between_short_waits(self) -> None:
        future = Future()
        signal = Event()

        def wait(pending, timeout, return_when):
            self.assertLessEqual(timeout, 1)
            signal.set()
            return set(), pending

        with patch("lncrawl.services.crawl_engine.wait", side_effect=wait):
            with self.assertRaises(AbortedException):
                list(crawl_items(["chapter"], lambda _: future, signal))
        self.assertTrue(future.cancelled())

    def test_successful_completions_are_counted(self) -> None:
        futures = [Future(), Future()]
        for future in futures:
            future.set_result("downloaded")
        progress = MagicMock()
        outcomes = list(
            crawl_items(["0", "1"], lambda item: futures[int(item)], Event(), on_progress=progress)
        )
        self.assertEqual(len(outcomes), 2)
        progress.assert_called_with(2)


class ArchiveResumeTests(unittest.TestCase):
    def test_checkpoint_write_failure_is_reported_instead_of_ignored(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ctx = SimpleNamespace(files=SimpleNamespace(resolve=lambda path: root / path))
            module = "lncrawl.services.scheduler.handlers.export_source"
            with (
                patch(f"{module}.ctx", ctx),
                patch(f"{module}.atomic_write", side_effect=OSError("Disk full")),
            ):
                with self.assertRaisesRegex(
                    HandlerException, "Could not save export checkpoint.*Disk full"
                ):
                    _save_manifest("novelfire.net", {})

    def test_missing_completed_ebook_is_recovered_from_legacy_zip(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            exports = root / "exports"
            exports.mkdir()
            archive = exports / "previous-export.zip"
            with zipfile.ZipFile(archive, "w") as book:
                book.writestr("Novel.epub", b"previously exported ebook")
                book.writestr(
                    "index.html",
                    _build_index_html("novelfire.net", [("Novel.epub", "Novel", "complete")]),
                )
            record = {"file": str(root / "removed" / "Novel.epub"), "missing": 0}
            ctx = SimpleNamespace(
                files=SimpleNamespace(resolve=lambda path: root / path),
                logger=SimpleNamespace(debug=MagicMock()),
            )
            with patch("lncrawl.services.scheduler.handlers.export_source.ctx", ctx):
                _restore_archived_books(
                    "novelfire.net", {"https://novelfire.net/book/novel": record}
                )
            self.assertEqual(Path(record["file"]).read_bytes(), b"previously exported ebook")
            self.assertEqual(Path(record["file"]).name, "Novel.epub")
            self.assertEqual(record["archive"], str(archive))

    def test_partial_or_wrong_source_archives_are_not_reused(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "exports").mkdir()
            for name, domain, status in [
                ("partial", "novelfire.net", "incomplete"),
                ("other", "example.com", "complete"),
            ]:
                with zipfile.ZipFile(root / "exports" / f"{name}.zip", "w") as book:
                    book.writestr("Novel.epub", b"not a complete book from this source")
                    book.writestr(
                        "index.html", _build_index_html(domain, [("Novel.epub", "Novel", status)])
                    )
            original = str(root / "missing" / "Novel.epub")
            record = {"file": original, "missing": 0}
            ctx = SimpleNamespace(
                files=SimpleNamespace(resolve=lambda path: root / path),
                logger=SimpleNamespace(debug=MagicMock()),
            )
            with patch("lncrawl.services.scheduler.handlers.export_source.ctx", ctx):
                _restore_archived_books("novelfire.net", {"novel-url": record})
            self.assertEqual(record["file"], original)
            self.assertFalse(Path(original).exists())


if __name__ == "__main__":
    unittest.main()
