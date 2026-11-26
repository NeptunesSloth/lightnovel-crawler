from collections import Counter
from concurrent.futures import Future
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from lncrawl.core import Chapter, Volume
from lncrawl.core.cleaner import TextCleaner
from sources.en.n.novelfire import NovelFireCrawler


class NovelFireTests(unittest.TestCase):
    def test_initialization_preserves_worker_count_and_request_limit(self) -> None:
        crawler = SimpleNamespace(workers=4, init_executor=MagicMock())
        NovelFireCrawler.initialize(crawler)
        crawler.init_executor.assert_called_once_with(workers=4, ratelimit=3)

    def test_chapter_list_pages_are_all_read_once_and_in_order(self) -> None:
        # Nine pages, each paginated with a window of the next three only, so the
        # crawler must keep following "next" past what the first page names.
        self._read_chapter_list(endless=False)

    def test_a_next_link_that_never_ends_stops_at_the_last_chapters(self) -> None:
        # Past its last page the list keeps linking "next" to empty pages (or to
        # the last one again): read for hours, the novel was "loading" all night.
        self._read_chapter_list(endless=True)

    def test_links_to_the_sites_own_pages_are_not_chapters(self) -> None:
        # A chapter list that came with the site's menus saved /home, /ranking
        # and the genre pages as chapters, each fetched empty on every run.
        self._read_chapter_list(endless=False, menus=True)

    def _read_chapter_list(self, endless: bool, menus: bool = False) -> None:
        novel_url = "https://novelfire.net/book/example"
        last_page = 9
        fetched: Counter = Counter()

        def page_html(number: int) -> str:
            links = "".join(
                f'<a class="page-link" href="/book/example/chapters?page={n}"'
                + (' rel="next"' if n == number + 1 else "")
                + f">{n}</a>"
                for n in range(max(1, number - 1), (number + 3 if endless else last_page) + 1)
                if n != number and (endless or n <= number + 3)
            )
            shown = min(number, last_page)
            items = "".join(
                f'<li><a title="Chapter {shown}.{i}" href="/book/example/c-{shown}-{i}">x</a></li>'
                for i in (1, 2)
                if number <= last_page or number % 2  # past the end: empty, or the last again
            )
            if menus:
                items += (
                    '<li><a title="Novel Fire" href="/home">h</a></li>'
                    '<li><a title="Ranking" href="https://novelfire.net/ranking">r</a></li>'
                    '<li><a title="Fantasy" href="/genre-fantasy/sort-popular">g</a></li>'
                    '<li><a title="Privacy" href="/page/privacy-policy">p</a></li>'
                    '<li><a title="Contact" href="/contact-us">c</a></li>'
                )
            return f'<ul class="chapter-list">{items}</ul><div class="pagination">{links}</div>'

        def get_soup(url: str) -> BeautifulSoup:
            fetched[url] += 1
            if url == novel_url:
                return BeautifulSoup(
                    '<h1>Example</h1><span itemprop="author">A</span>'
                    '<div class="cover"><img src="/c.jpg"></div>',
                    "html.parser",
                )
            number = int(url.rsplit("page=", 1)[1]) if "page=" in url else 1
            return BeautifulSoup(page_html(number), "html.parser")

        def submit_task(fn, *args) -> Future:
            future: Future = Future()
            future.set_result(fn(*args))
            return future

        crawler = SimpleNamespace(
            novel_url=novel_url,
            get_soup=get_soup,
            submit_task=submit_task,
            absolute_url=lambda url: urljoin(novel_url, url),
            cleaner=self.cleaner,
            chapters=[],
            volumes=[],
        )
        NovelFireCrawler.read_novel_info(crawler)

        self.assertEqual(
            [chapter.title for chapter in crawler.chapters],
            [f"Chapter {n}.{i}" for n in range(1, last_page + 1) for i in (1, 2)],
        )
        self.assertEqual([chapter.id for chapter in crawler.chapters], list(range(1, 19)))
        self.assertEqual(crawler.volumes, [Volume(id=n) for n in range(1, last_page + 1)])
        self.assertEqual(
            [chapter.volume for chapter in crawler.chapters],
            [n for n in range(1, last_page + 1) for _ in (1, 2)],
        )
        self.assertTrue(all(count == 1 for count in fetched.values()), fetched)
        if endless:
            # one round of pages past the end is read before it is known to be
            self.assertLess(len(fetched), last_page + 6, fetched)
        else:
            self.assertEqual(len(fetched), last_page + 1)  # the novel page and each list page

    @classmethod
    def setUpClass(cls) -> None:
        cls.cleaner = TextCleaner()

    def extract(self, html: str) -> str:
        crawler = SimpleNamespace(cleaner=self.cleaner)
        chapter = Chapter(
            id=12, title="Chapter 12: Arrival", url="https://novelfire.net/book/example/chapter-12"
        )
        soup = BeautifulSoup(html, "html.parser")
        return NovelFireCrawler._chapter_body(crawler, soup, chapter)

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
