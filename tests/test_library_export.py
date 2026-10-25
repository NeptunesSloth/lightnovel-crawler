from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import zipfile

from lncrawl.services.library_export import build_library_zip


def ebook(text):
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("chapter.xhtml", text)
    return output.getvalue()


class LibraryExportTests(unittest.TestCase):
    def test_includes_archived_books_deduplicates_and_links_collision_names(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            books = root / "novels" / "one"
            books.mkdir(parents=True)
            first = ebook("story one")
            (books / "Story.epub").write_bytes(first)
            exports = root / "exports"
            exports.mkdir()
            with zipfile.ZipFile(exports / "old.zip", "w") as archive:
                archive.writestr("Story.epub", first)
                archive.writestr("nested/Story.epub", ebook("story two"))
                archive.writestr("broken.epub", b"invalid")
            output = root / "library.zip"
            self.assertEqual(build_library_zip(root, output), 2)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {"books/Story.epub", "books/Story (2).epub", "index.html", "README.txt"},
                )
                index = archive.read("index.html").decode()
                self.assertIn('href="books/Story%20%282%29.epub"', index)
                self.assertIn("completeness not checked", index)
                self.assertEqual(archive.read("books/Story.epub"), first)

    def test_empty_library_has_index_and_no_books(self):
        with TemporaryDirectory() as temp:
            output = Path(temp) / "library.zip"
            self.assertEqual(build_library_zip(Path(temp), output), 0)
            with zipfile.ZipFile(output) as archive:
                self.assertIn("index.html", archive.namelist())
