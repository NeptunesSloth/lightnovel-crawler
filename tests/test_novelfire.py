from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from bs4 import BeautifulSoup

from lncrawl.core import Chapter
from lncrawl.core.cleaner import TextCleaner
from sources.en.n.novelfire import NovelFireCrawler


class NovelFireTests(unittest.TestCase):
    def test_initialization_preserves_worker_count_and_request_limit(self) -> None:
        crawler = SimpleNamespace(workers=4, init_executor=MagicMock())
        NovelFireCrawler.initialize(crawler)
        crawler.init_executor.assert_called_once_with(workers=4, ratelimit=3)

    @classmethod
    def setUpClass(cls) -> None:
        cls.cleaner = TextCleaner()

    def extract(self, html: str) -> str:
        crawler = SimpleNamespace(
            get_soup=lambda url: BeautifulSoup(html, "html.parser"),
            cleaner=self.cleaner,
        )
        chapter = Chapter(
            id=12, title="Chapter 12: Arrival", url="https://novelfire.net/book/example/chapter-12"
        )
        return NovelFireCrawler.download_chapter_body(crawler, chapter)

    def test_nested_wrapper_keeps_all_story_paragraphs(self) -> None:
        text = self.extract(
            '<div id="content"><div class="chapter-text">'
            "<h2>Chapter 12: Arrival</h2><p>The first paragraph.</p>"
            "<p>The next paragraph.</p></div></div>"
        )
        self.assertIn("The first paragraph.", text)
        self.assertIn("The next paragraph.", text)
        self.assertNotIn("Chapter 12", text)

    def test_paragraph_starting_with_chapter_is_not_deleted(self) -> None:
        text = self.extract(
            '<div id="content"><p>Chapter 12 was the last thing he remembered reading.</p>'
            "<p>He opened his eyes.</p></div>"
        )
        self.assertIn("Chapter 12 was the last thing he remembered reading.", text)
        self.assertIn("He opened his eyes.", text)

    def test_standalone_title_paragraph_is_removed(self) -> None:
        text = self.extract(
            '<div id="content"><p><strong>Chapter 12: Arrival</strong></p>'
            "<p>The story begins.</p></div>"
        )
        self.assertNotIn("Chapter 12", text)
        self.assertIn("The story begins.", text)

    def test_missing_container_returns_empty_body(self) -> None:
        self.assertEqual(self.extract("<html><body>Unavailable</body></html>"), "")


if __name__ == "__main__":
    unittest.main()
