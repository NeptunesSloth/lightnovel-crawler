import unittest

from bs4 import BeautifulSoup
from scraper import PageSoup

from lncrawl.utils.novel_status import detect, from_page, from_tags, normalize


def page(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


class NovelStatusTests(unittest.TestCase):
    def test_normalizes_what_sources_print(self) -> None:
        self.assertEqual(normalize("OnGoing"), "ongoing")
        self.assertEqual(normalize("Completed (Scan)"), "completed")
        self.assertEqual(normalize("連載"), "ongoing")
        self.assertEqual(normalize("dropped"), "cancelled")
        self.assertEqual(normalize("Hiatus"), "hiatus")
        self.assertIsNone(normalize("2024"))
        self.assertIsNone(normalize(None))

    def test_tags_match_whole_words_only(self) -> None:
        self.assertEqual(from_tags(["Action", "Completed"]), "completed")
        self.assertIsNone(from_tags(["Full Color", "The End of Days"]))

    def test_reads_status_beside_its_label(self) -> None:
        layouts = {
            # NovelFire: value over a small caption
            "<span><strong>Ongoing</strong> <small>Status</small></span>": "ongoing",
            # Weeb Central: label then a search link
            '<li><strong>Status: </strong> <a href="?s=Hiatus">Hiatus</a></li>': "hiatus",
            # Madara: heading and content in separate blocks
            '<div><div class="summary-heading"><h5>Status</h5></div>'
            '<div class="summary-content">OnGoing</div></div>': "ongoing",
            "<table><tr><th>Status</th><td>Completed</td></tr></table>": "completed",
            "<dl><dt>Status:</dt><dd>Finished</dd></dl>": "completed",
            "<p>Status: Completed</p>": "completed",
        }
        for html, expected in layouts.items():
            with self.subTest(html=html):
                self.assertEqual(from_page(page(html)), expected)
                self.assertEqual(from_page(PageSoup(page(html))), expected)

    def test_ignores_labels_without_a_status(self) -> None:
        self.assertIsNone(from_page(page("<p>Status</p><p>Read now</p>")))
        self.assertIsNone(from_page(page("<p>Status: we are hiring</p>")))

    def test_detect_prefers_the_source_then_tags_then_last_chapter(self) -> None:
        self.assertEqual(detect("Ongoing", ["Completed"], "Ch.9 (The End)"), "ongoing")
        self.assertEqual(detect(None, ["Completed"], ""), "completed")
        self.assertEqual(detect(None, [], "Ch.15 Side Story 5 (The End)"), "completed")
        self.assertIsNone(detect(None, [], "Chapter 3: The End of the World"))


if __name__ == "__main__":
    unittest.main()


class LegacyStatusFromPageTests(unittest.TestCase):
    """Sources that never read a status get it from the novel page they fetched."""

    PAGES = {
        "https://fake.example/n/1": "<h1>Book</h1><p><span>Status:</span> <span>Completed</span></p>",
        "https://fake.example/n/1/chapters": "<p>Status: Ongoing</p>",
    }

    def crawl(self, own_status=None):
        from lncrawl.core import LegacyCrawler, Novel as CrawlerNovel

        class Fake(LegacyCrawler):
            base_url = ["https://fake.example/"]

            def read_novel_info(self):
                soup = self.get_soup(self.novel_url)
                self.novel_title = soup.select_one("h1").text
                self.get_soup(self.novel_url + "/chapters")
                if own_status:
                    self.novel_status = own_status

            def download_chapter_body(self, chapter):
                return ""

        crawler = Fake()
        crawler.scraper.get_soup = lambda url, *a, **k: PageSoup(page(self.PAGES[url]))
        model = CrawlerNovel(url="https://fake.example/n/1")
        crawler.read_novel(model)
        return model

    def test_reads_status_from_the_novel_page_only(self) -> None:
        self.assertEqual(self.crawl().get("novel_status"), "completed")

    def test_keeps_the_status_the_source_read(self) -> None:
        self.assertEqual(self.crawl("hiatus").get("novel_status"), "hiatus")
