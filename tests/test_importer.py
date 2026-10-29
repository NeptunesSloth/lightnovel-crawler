from io import BytesIO
import unittest
import zipfile

from lncrawl.services.importer import _RE_NOT_A_BOOK, _source_url, parse_cbz


def cbz(*names):
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name in names:
            archive.writestr(name, b"jpg")
    return output.getvalue()


class ImporterParsingTests(unittest.TestCase):
    def test_source_url_prefers_footer_over_synopsis_links(self):
        intro = (
            '<div id="intro"><div class="synopsis"><a href="https://discord.gg/x">Join</a></div>'
            '<div class="footer"><b>Source:</b> <a href="https://site.com/novel/a">a</a>'
            '<a href="https://github.com/lncrawl/lightnovel-crawler">LNCrawl</a></div></div>'
        )
        self.assertEqual(_source_url(intro), "https://site.com/novel/a")

    def test_readme_beside_books_is_not_a_book(self):
        self.assertTrue(_RE_NOT_A_BOOK.match("README.txt"))
        self.assertFalse(_RE_NOT_A_BOOK.match("Reader's Guide.epub"))

    def test_cbz_written_by_this_app_comes_back_by_chapter(self):
        data = cbz("00000_0000_cover.jpg", "00001_0001.jpg", "00001_0002.jpg", "00002_0001.jpg")
        book = parse_cbz(data, "My Manga.cbz")
        assert book is not None
        self.assertEqual(book.title, "My Manga")
        self.assertTrue(book.manga)
        self.assertEqual(book.cover, b"jpg")
        self.assertEqual([c.serial for c in book.chapters], [1, 2])
        self.assertEqual(len(book.chapters[0].images), 2)
        self.assertIn(f"images/{book.chapters[0].images[0]}.jpg", book.chapters[0].body)
        self.assertEqual(len(book.images), 3)

    def test_other_cbz_has_one_chapter_per_folder_in_natural_order(self):
        data = cbz("Ch 10/1.jpg", "Ch 2/2.png", "Ch 2/10.png", "Ch 2/1.png")
        book = parse_cbz(data, "Other.cbz")
        assert book is not None
        self.assertEqual([c.title for c in book.chapters], ["Ch 2", "Ch 10"])
        self.assertEqual(
            [book.images[i] for i in book.chapters[0].images],
            ["Ch 2/1.png", "Ch 2/2.png", "Ch 2/10.png"],
        )
