"""A ZIP of a whole library imported into a fresh install, where it is.

Dropped on the page, a library ZIP of hundreds of gigabytes was copied twice on
its way in (by the web server to the system's temp folder, then into the
library) and refused past 8 GB, on a fresh install whose library is on the
system drive. Chosen on the computer instead, it is read where it is.
"""

import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from lncrawl.utils import folders

ROOT = Path(__file__).resolve().parents[1]


def epub(title: str, url: str, chapters: int) -> bytes:
    from io import BytesIO

    output = BytesIO()
    items = "".join(
        f'<item href="chapter_{i:05}.xhtml" id="c{i}" media-type="application/xhtml+xml"/>'
        for i in range(1, chapters + 1)
    )
    spine = "".join(f'<itemref idref="c{i}"/>' for i in range(1, chapters + 1))
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="EPUB/content.opf"/></rootfiles></container>',
        )
        archive.writestr(
            "EPUB/content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<dc:identifier>{url}</dc:identifier><dc:title>{title}</dc:title></metadata>"
            '<manifest><item href="intro.xhtml" id="intro" media-type="application/xhtml+xml"/>'
            f"{items}</manifest><spine>{spine}</spine></package>",
        )
        archive.writestr(
            "EPUB/intro.xhtml",
            f'<html><body><div class="footer"><a href="{url}">{url}</a></div></body></html>',
        )
        for i in range(1, chapters + 1):
            archive.writestr(
                f"EPUB/chapter_{i:05}.xhtml",
                f"<html><body><h1>Chapter {i}</h1><p>{title}, chapter {i}.</p></body></html>",
            )
    return output.getvalue()


def library_zip(path: Path, count: int) -> None:
    """Like the one Make a ZIP writes: books/, an index and a README."""
    with zipfile.ZipFile(path, "w") as archive:
        for n in range(count):
            archive.writestr(f"books/Novel {n}.epub", epub(f"Novel {n}", f"https://s.com/{n}", 3))
        archive.writestr("index.html", "<html></html>")
        archive.writestr("README.txt", "Extract this ZIP")


IMPORT = """
import sys
from threading import Event
from lncrawl.context import ctx
ctx.setup(sync_remote_index=False)
from lncrawl.dao import Novel, User
from lncrawl.server.api.admin import import_file
from lncrawl.server.models.library import ImportFileRequest
from lncrawl.services.scheduler.handlers import run_job
import sqlmodel as sq
with ctx.db.session() as session:
    admin = session.exec(sq.select(User).where(User.role == "admin")).first()
job = import_file(body=ImportFileRequest(path=sys.argv[1]), user=admin)
run_job(ctx.jobs.get(job.id), Event())
job = ctx.jobs.get(job.id)
print("STATUS", job.status, job.error)
print("BOOKS", job.extra.get("books_read"), "of", job.extra.get("books"))
print("IMPORTED", job.extra.get("imported"), "CHAPTERS", job.extra.get("chapters"))
with ctx.db.session() as session:
    print("NOVELS", len(session.exec(sq.select(Novel)).all()))
print("UPLOADS", sorted(p.name for p in ctx.files.resolve("imports").glob("*")) if ctx.files.resolve("imports").exists() else [])
ctx.destroy()
"""


class ImportInPlaceTests(unittest.TestCase):
    def test_a_library_zip_is_imported_where_it_is_into_a_fresh_install(self) -> None:
        with TemporaryDirectory() as temp:
            data = Path(temp) / "fresh" / "LNCrawl"
            archive = Path(temp) / "ssd" / "library.zip"
            archive.parent.mkdir()
            library_zip(archive, 5)
            before = archive.stat()
            env = dict(os.environ, LNCRAWL_DATA_PATH=str(data))
            env.pop("DATABASE_URL", None)
            result = subprocess.run(
                [sys.executable, "-c", IMPORT, str(archive)],
                env=env,
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=180,
            )
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-3000:])
            out = result.stdout
            self.assertIn("BOOKS 5 of 5", out)
            self.assertIn("IMPORTED 5 CHAPTERS 15", out)
            self.assertIn("NOVELS 5", out)
            self.assertIn("UPLOADS []", out)  # nothing was copied in to be read
            self.assertTrue(archive.is_file())  # and the file is the user's: left as it was
            self.assertEqual(archive.stat().st_size, before.st_size)


class ImportFileChecksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / "library.zip"
        library_zip(self.archive, 1)
        import importlib

        self.admin = importlib.import_module("lncrawl.server.api.admin")
        self.jobs = SimpleNamespace(import_archive=MagicMock(return_value="job"))
        ctx = SimpleNamespace(
            config=SimpleNamespace(app=SimpleNamespace(app_dir=self.root)), jobs=self.jobs
        )
        patcher = patch.object(self.admin, "ctx", ctx)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, path: str):
        from lncrawl.server.models.library import ImportFileRequest

        return self.admin.import_file(body=ImportFileRequest(path=path), user="admin")

    def test_it_is_queued_to_be_read_where_it_is_and_kept(self) -> None:
        self.assertEqual(self.call(str(self.archive)), "job")
        _, kwargs = self.jobs.import_archive.call_args
        self.assertEqual(kwargs["archive_file"], str(self.archive))
        self.assertTrue(kwargs["keep_archive"])

    def test_not_enough_room_is_said_before_anything_is_read(self) -> None:
        from lncrawl.exceptions import ServerError

        full = SimpleNamespace(free=1024, total=10**12)
        with patch.object(self.admin.shutil, "disk_usage", return_value=full):
            with self.assertRaises(ServerError) as caught:
                self.call(str(self.archive))
        self.assertIn("Move the library to a bigger drive", caught.exception.extra or "")
        self.jobs.import_archive.assert_not_called()

    def test_what_is_not_a_file_to_import_is_refused(self) -> None:
        from lncrawl.exceptions import ServerError

        (self.root / "notes.txt.bak").write_text("x")
        for path in (
            str(self.root / "missing.zip"),
            str(self.root / "notes.txt.bak"),
            "relative.zip",
        ):
            with self.assertRaises(ServerError, msg=path):
                self.call(path)

    def test_the_picker_lists_the_files_to_import_beside_the_folders(self) -> None:
        (self.root / "Novels").mkdir()
        (self.root / "Story.epub").write_bytes(b"epub")
        (self.root / "photo.jpg").write_bytes(b"jpg")
        listed = folders.list_folder(str(self.root), (".zip", ".epub"))
        self.assertEqual([f["name"] for f in listed["folders"]], ["Novels"])
        self.assertEqual([f["name"] for f in listed["files"]], ["library.zip", "Story.epub"])
        self.assertEqual(folders.list_folder(str(self.root))["files"], [])


if __name__ == "__main__":
    unittest.main()
