"""Choosing the folder a site export's ZIP is written to (a roomier drive)."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from lncrawl.dao import UserRole
from lncrawl.exceptions import ServerError
from lncrawl.server.api.jobs import _export_path, export_source
from lncrawl.server.models import ExportSourceRequest
from lncrawl.services.scheduler.handlers.export_source import (
    _build_index_html,
    _export_target,
    _restore_archived_books,
)
from lncrawl.utils.file_tools import writable_folder

MODULE = "lncrawl.services.scheduler.handlers.export_source"


class WritableFolderTests(unittest.TestCase):
    def test_a_missing_folder_is_made(self) -> None:
        with TemporaryDirectory() as directory:
            folder = Path(directory) / "exports" / "novels"
            self.assertEqual(writable_folder(str(folder)), folder)
            self.assertTrue(folder.is_dir())
            self.assertEqual(list(folder.iterdir()), [])  # the write check left nothing

    def test_a_relative_path_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            writable_folder("exports")

    def test_a_file_is_not_a_folder(self) -> None:
        with TemporaryDirectory() as directory:
            file = Path(directory) / "file.txt"
            file.write_text("x")
            with self.assertRaises(OSError):
                writable_folder(str(file))


class ExportTargetTests(unittest.TestCase):
    def test_without_a_folder_the_zip_stays_in_the_data_folder(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            ctx = SimpleNamespace(files=SimpleNamespace(resolve=lambda path: root / path))
            with patch(f"{MODULE}.ctx", ctx):
                stored, path = _export_target(None, "novelfire.net", "job-1234567890")
            self.assertEqual(stored, "exports/job-1234567890.zip")
            self.assertEqual(path, root / "exports" / "job-1234567890.zip")

    def test_a_chosen_folder_gets_the_zip_itself_without_replacing_another(self) -> None:
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            stored, path = _export_target(folder, "novelfire.net", "job-1234567890")
            self.assertEqual(path, folder / "novelfire.net-novels.zip")
            self.assertEqual(stored, str(path))
            path.write_bytes(b"an earlier export")
            _, again = _export_target(folder, "novelfire.net", "job-1234567890")
            self.assertEqual(again, folder / "novelfire.net-novels-job-1234.zip")


class SavedArchiveResumeTests(unittest.TestCase):
    def test_books_are_recovered_from_a_zip_saved_in_a_chosen_folder(self) -> None:
        with TemporaryDirectory() as data, TemporaryDirectory() as chosen:
            root = Path(data)
            archive = Path(chosen) / "novelfire.net-novels.zip"
            with zipfile.ZipFile(archive, "w") as book:
                book.writestr("Novel.epub", b"previously exported ebook")
                book.writestr(
                    "index.html",
                    _build_index_html("novelfire.net", [("Novel.epub", "Novel", "complete")]),
                )
            record = {
                "file": str(root / "removed" / "Novel.epub"),
                "missing": 0,
                "archive": str(archive),
                "archive_name": "Novel.epub",
            }
            ctx = SimpleNamespace(
                files=SimpleNamespace(resolve=lambda path: root / path),
                logger=SimpleNamespace(debug=MagicMock()),
            )
            with patch(f"{MODULE}.ctx", ctx):
                _restore_archived_books("novelfire.net", {"https://novelfire.net/book/n": record})
            self.assertEqual(Path(record["file"]).read_bytes(), b"previously exported ebook")


class ApiTests(unittest.TestCase):
    def test_only_an_admin_may_pick_a_folder_on_the_server(self) -> None:
        user = SimpleNamespace(id="u", role=UserRole.USER)
        body = ExportSourceRequest(url="https://novelfire.net/", save_dir="/tmp/anywhere")
        with self.assertRaises(ServerError) as caught:
            export_source(user=user, body=body)  # type: ignore[arg-type]
        self.assertEqual(caught.exception.status_code, 403)

    def test_a_folder_that_cannot_be_written_is_refused_before_anything_downloads(self) -> None:
        user = SimpleNamespace(id="u", role=UserRole.ADMIN)
        with TemporaryDirectory() as directory:
            file = Path(directory) / "file.txt"
            file.write_text("x")
            body = ExportSourceRequest(url="https://novelfire.net/", save_dir=str(file))
            jobs = SimpleNamespace(export_source=MagicMock())
            with patch("lncrawl.server.api.jobs.ctx", SimpleNamespace(jobs=jobs)):
                with self.assertRaises(ServerError) as caught:
                    export_source(user=user, body=body)  # type: ignore[arg-type]
            self.assertEqual(caught.exception.status_code, 422)
            jobs.export_source.assert_not_called()

    def test_the_chosen_folder_reaches_the_job(self) -> None:
        user = SimpleNamespace(id="u", role=UserRole.ADMIN)
        with TemporaryDirectory() as directory:
            body = ExportSourceRequest(url="https://novelfire.net/", save_dir=directory)
            jobs = SimpleNamespace(export_source=MagicMock())
            with patch("lncrawl.server.api.jobs.ctx", SimpleNamespace(jobs=jobs)):
                export_source(user=user, body=body)  # type: ignore[arg-type]
            self.assertEqual(jobs.export_source.call_args.kwargs["save_dir"], directory)

    def test_a_zip_saved_outside_the_data_folder_can_still_be_downloaded(self) -> None:
        with TemporaryDirectory() as directory:
            outside = Path(directory) / "novelfire.net-novels.zip"
            files = SimpleNamespace(resolve=lambda path: Path("/data") / str(path).lstrip("/"))
            with patch("lncrawl.server.api.jobs.ctx", SimpleNamespace(files=files)):
                self.assertEqual(_export_path(str(outside)), outside)
                self.assertEqual(_export_path("exports/job.zip"), Path("/data/exports/job.zip"))


if __name__ == "__main__":
    unittest.main()
