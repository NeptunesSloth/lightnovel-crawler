"""AI universal crawler — works on (almost) any novel/manga site.

Instead of a hand-written per-source crawler, an LLM infers the CSS selectors for
a site once, and the proven ``SoupTemplate`` machinery does the actual extraction.
Used as a fallback when a URL's domain has no registered crawler (and an OpenAI
key is configured). At most two model calls per novel: one for the chapter-list
selectors, one for the chapter-content selector; everything else is deterministic
BeautifulSoup, with a readability fallback if a selector misbehaves.
"""

import logging
from typing import Iterable, Optional

from ..exceptions import LNException
from . import ai_extract
from .models import Chapter, Novel, SearchResult
from .template import SoupTemplate

logger = logging.getLogger(__name__)


class AIUniversalCrawler(SoupTemplate):
    has_manga = True
    has_mtl = False

    def __init__(self, origin: Optional[str] = None, **kwargs) -> None:
        # universal: the base_url is whatever site we were pointed at
        self.base_url = [origin] if origin else [""]
        super().__init__(origin=origin, **kwargs)
        self._novel_soup = None
        self._content_selector = ""
        self._content_known = False

    # No catalogue search — this crawler works from a direct novel URL.
    def search(self, query: str) -> Iterable[SearchResult]:
        return []

    def get_novel_soup(self, novel: Novel):
        # cache so inferring selectors and parsing share a single page fetch
        if self._novel_soup is None:
            self._novel_soup = self.scraper.get_soup(self.build_novel_url(novel))
        return self._novel_soup

    def read_novel(self, novel: Novel) -> None:
        soup = self.get_novel_soup(novel)
        try:
            sels = ai_extract.infer_novel_selectors(str(soup))
        except Exception as e:
            raise LNException(f"AI could not analyze the page: {e}") from e
        if not sels.get("chapter_list"):
            raise LNException("AI could not find a chapter list on this page")
        self.chapter_list_selector = sels["chapter_list"]
        if sels.get("chapter_title"):
            self.chapter_title_selector = sels["chapter_title"]
        if sels.get("chapter_url"):
            self.chapter_url_selector = sels["chapter_url"]
        if sels.get("novel_title"):
            self.novel_title_selector = sels["novel_title"]
        if sels.get("novel_cover"):
            self.novel_cover_selector = sels["novel_cover"]
        logger.info("AI crawler selectors for %s: %s", novel.url, sels)
        super().read_novel(novel)

    # Empty selectors are valid here (the row may itself be the <a>); guard the
    # select_one calls so an empty string doesn't raise.
    def parse_chapter_title(self, soup, chapter: Chapter) -> None:
        tag = soup
        if self.chapter_title_selector:
            tag = soup.select_one(self.chapter_title_selector) or soup
        chapter.title = (tag.text or "").strip()

    def parse_chapter_url(self, soup, chapter: Chapter) -> None:
        if self.chapter_url_selector:
            tag = soup.select_one(self.chapter_url_selector) or soup
        elif getattr(soup, "name", None) == "a":
            tag = soup
        else:
            tag = soup.select_one("a") or soup
        chapter.url = self.absolute_url(tag.get("href"))

    def download_chapter(self, chapter: Chapter) -> None:
        url = self.build_chapter_url(chapter)
        soup = self.scraper.get_soup(url)
        if not self._content_known:
            self._content_known = True
            try:
                self._content_selector = ai_extract.infer_content_selector(str(soup))
            except Exception:
                self._content_selector = ""
        body = None
        if self._content_selector:
            try:
                body = soup.select_one(self._content_selector)
            except Exception:
                body = None
        if body is None:
            body = ai_extract.readability_content(soup)
        if body is None:
            body = soup.select_one("body") or soup
        self.parse_chapter_body(body, chapter)
