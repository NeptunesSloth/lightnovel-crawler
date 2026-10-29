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
