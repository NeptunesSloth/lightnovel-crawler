from contextlib import nullcontext
import importlib
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
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

    def test_nothing_is_written_to_the_system_temp_folder(self):
        # With the system drive full, every export ended in "Internal Server
        # Error": each book was copied through the temp folder to be read.
        with TemporaryDirectory() as temp:
            root = Path(temp) / "data"
            books = root / "novels" / "one"
            books.mkdir(parents=True)
            (books / "Story.epub").write_bytes(ebook("story one"))
            (root / "exports").mkdir()
            with zipfile.ZipFile(root / "exports" / "old.zip", "w") as archive:
                archive.writestr("Other.epub", ebook("story two"))
            save = Path(temp) / "other-drive"
            save.mkdir()
            full = Path(temp) / "full-system-temp"  # does not exist: any write there fails
            with patch("tempfile.tempdir", str(full)):
                self.assertEqual(build_library_zip(root, save / "library.zip"), 2)
            self.assertEqual([p.name for p in save.iterdir()], ["library.zip"])

    def test_empty_library_has_index_and_no_books(self):
        with TemporaryDirectory() as temp:
            output = Path(temp) / "library.zip"
            self.assertEqual(build_library_zip(Path(temp), output), 0)
            with zipfile.ZipFile(output) as archive:
                self.assertIn("index.html", archive.namelist())


def novel_epub(title, url, chapters, tags=(), modified="2026-01-01T00:00:00Z"):
    output = BytesIO()
    subjects = "".join(f"<dc:subject>{t}</dc:subject>" for t in tags)
    items = "".join(
        f'<item href="chapter_{i:05}.xhtml" id="c{i}" media-type="application/xhtml+xml"/>'
        for i in range(1, chapters + 1)
    )
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
            f"<dc:identifier>{url}</dc:identifier><dc:title>{title}</dc:title>{subjects}"
            f'<meta property="dcterms:modified">{modified}</meta></metadata><manifest>'
            '<item href="intro.xhtml" id="intro" media-type="application/xhtml+xml"/>'
            f"{items}</manifest><spine/></package>",
        )
        archive.writestr(
            "EPUB/intro.xhtml",
            f'<div id="intro"><div class="synopsis"><a href="https://discord.gg/x">join</a></div>'
            f'<div class="footer"><a href="{url}">{url}</a></div></div>',
        )
    return output.getvalue()


class LibraryExportByNovelTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def save(self, rel, data):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def books(self, **kwargs):
        output = self.root / "library.zip"
        count = build_library_zip(self.root, output, **kwargs)
        with zipfile.ZipFile(output) as archive:
            names = sorted(n for n in archive.namelist() if n.startswith("books/"))
            return count, names, archive

    def test_keeps_one_copy_per_novel_the_most_complete(self):
        url = "https://site.com/novel/a"
        self.save("novels/1/artifacts/A.epub", novel_epub("A", url, 3))
        newer = novel_epub("A", url, 5, modified="2026-02-01T00:00:00Z")
        (self.root / "exports").mkdir()
        with zipfile.ZipFile(self.root / "exports" / "old.zip", "w") as archive:
            archive.writestr("A.epub", newer)
        # a different novel that shares a synopsis link stays separate
        self.save("novels/2/artifacts/B.epub", novel_epub("B", "https://site.com/novel/b", 2))
        count, names, _ = self.books()
        self.assertEqual(count, 2)
        self.assertEqual(names, ["books/A.epub", "books/B.epub"])
        with zipfile.ZipFile(self.root / "library.zip") as archive:
            self.assertEqual(archive.read("books/A.epub"), newer)

    def test_includes_cbz_manga(self):
        output = BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("00001_0001.jpg", b"jpg")
        self.save("novels/3/artifacts/M.cbz", output.getvalue())
        count, names, _ = self.books()
        self.assertEqual(names, ["books/M.cbz"])

    def test_nsfw_filter_uses_book_and_library_tags(self):
        self.save("novels/1/artifacts/Safe.epub", novel_epub("Safe", "https://s.com/1", 1))
        self.save(
            "novels/2/artifacts/Spicy.epub",
            novel_epub("Spicy", "https://s.com/2", 1, tags=["Romance", "R-18"]),
        )
        cbz = BytesIO()
        with zipfile.ZipFile(cbz, "w") as archive:
            archive.writestr("00001_0001.jpg", b"jpg")
        self.save("novels/3/artifacts/Comic.cbz", cbz.getvalue())
        tags = {"3": ["Ecchi"]}

        def tags_for(novel_id):
            return tags.get(novel_id, [])

        _, names, _ = self.books(nsfw="exclude", tags_for=tags_for)
        self.assertEqual(names, ["books/Safe.epub"])
        _, names, _ = self.books(nsfw="only", tags_for=tags_for)
        self.assertEqual(names, ["books/Comic.cbz", "books/Spicy.epub"])
        _, names, _ = self.books(tags_for=tags_for)
        self.assertEqual(len(names), 3)


class FullyDownloadedOnlyTests(LibraryExportByNovelTests):
    def test_only_novels_with_every_chapter_go_in(self):
        self.save("novels/1/artifacts/Whole.epub", novel_epub("Whole", "https://s.com/1", 3))
        self.save("novels/2/artifacts/Partly.epub", novel_epub("Partly", "https://s.com/2", 2))
        # every chapter is in the library, but this book was built before the last ones
        self.save("novels/4/artifacts/Stale.epub", novel_epub("Stale", "https://s.com/4", 2))
        (self.root / "exports").mkdir()
        with zipfile.ZipFile(self.root / "exports" / "old.zip", "w") as archive:
            # kept only inside a past export: found by the novel's address
            archive.writestr("Kept.epub", novel_epub("Kept", "https://s.com/kept", 4))
        full = {"1": 3, "4": 5, "https://s.com/kept": 4}
        asked = []

        def full_length(novel_id, url):
            asked.append((novel_id, url))
            return full.get(novel_id or "") or full.get(url or "")

        left_out = []
        count, names, _ = self.books(full_length=full_length, left_out=left_out)
        self.assertEqual(names, ["books/Kept.epub", "books/Whole.epub"])
        self.assertEqual(count, 2)
        self.assertEqual(sorted(left_out), ["Partly", "Stale"])
        self.assertIn((None, "https://s.com/kept"), asked)
        with zipfile.ZipFile(self.root / "library.zip") as archive:
            index = archive.read("index.html").decode()
            self.assertNotIn("incomplete", index)
            self.assertNotIn("not checked", index)

    def test_a_library_zip_is_not_taken_for_a_past_export(self):
        (self.root / "exports").mkdir()
        with zipfile.ZipFile(self.root / "exports" / "library-earlier.zip", "w") as archive:
            archive.writestr("books/A.epub", novel_epub("A", "https://s.com/a", 1))
        count, names, _ = self.books()
        self.assertEqual((count, names), (0, []))


class LibraryExportApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "data"
        for rel, title, chapters in (("1", "Whole", 3), ("2", "Partly", 2)):
            path = self.root / "novels" / rel / "artifacts" / f"{title}.epub"
            path.parent.mkdir(parents=True)
            path.write_bytes(novel_epub(title, f"https://s.com/{rel}", chapters))
        reports = {"1": (True, 3), "2": (False, 5)}

        def completeness(novel_id):
            complete, total = reports[novel_id]
            return SimpleNamespace(complete=complete, total=total)

        session = SimpleNamespace(get=lambda *args: None)
        self.ctx = SimpleNamespace(
            files=SimpleNamespace(resolve=lambda path: self.root / path),
            novels=SimpleNamespace(find_by_url=lambda url: None),
            chapters=SimpleNamespace(completeness=completeness),
            db=SimpleNamespace(session=lambda: nullcontext(session)),
        )
        # the package re-exports the admin router under the module's own name
        admin = importlib.import_module("lncrawl.server.api.admin")
        patcher = patch.object(admin, "ctx", self.ctx)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_zip_is_written_to_the_chosen_folder_and_nowhere_else(self):
        from lncrawl.server.api.admin import export_library_to_folder
        from lncrawl.server.models.library import LibraryExportRequest

        folder = Path(self.temp.name) / "ssd" / "Novels"
        body = LibraryExportRequest(save_dir=str(folder), complete_only=True)
        first = export_library_to_folder(body=body)
        self.assertEqual(Path(first["file"]), folder / "library.zip")
        self.assertEqual((first["books"], first["left_out"]), (1, 1))
        self.assertEqual(first["left_out_titles"], ["Partly"])
        with zipfile.ZipFile(first["file"]) as archive:
            self.assertIn("books/Whole.epub", archive.namelist())
        second = export_library_to_folder(body=body)
        self.assertNotEqual(second["file"], first["file"])  # an earlier one is never replaced
        self.assertEqual(sorted(p.name for p in folder.iterdir() if p.suffix != ".zip"), [])
        self.assertFalse(
            (self.root / "exports").exists() and any((self.root / "exports").iterdir())
        )

    def test_a_folder_that_cannot_be_written_is_refused(self):
        from lncrawl.exceptions import ServerError
        from lncrawl.server.api.admin import export_library_to_folder
        from lncrawl.server.models.library import LibraryExportRequest

        file = Path(self.temp.name) / "file.txt"
        file.write_text("x")
        with self.assertRaises(ServerError) as caught:
            export_library_to_folder(body=LibraryExportRequest(save_dir=str(file)))
        self.assertEqual(caught.exception.status_code, 422)

    def test_a_download_is_built_in_the_data_folder_not_the_system_temp(self):
        from lncrawl.server.api.admin import export_library

        response = export_library(nsfw="include", complete_only=True)
        built = Path(response.path)
        self.addCleanup(built.unlink, missing_ok=True)
        self.assertEqual(built.parent, self.root / "exports")
        self.assertTrue(built.name.startswith("library-"))
        self.assertEqual(response.headers["x-books"], "1")
        self.assertEqual(response.headers["x-left-out"], "1")
