# -*- coding: utf-8 -*-
"""Crawler for https://mangakatana.com/ — a manga reader. Pages are emitted as
<img> tags so the engine downloads and embeds them."""

import logging
import re
from urllib.parse import quote_plus

from lncrawl.core import Chapter, LegacyCrawler, Volume

logger = logging.getLogger(__name__)

search_url = "https://mangakatana.com/?search=%s&search_by=book_name"


class MangaKatanaCrawler(LegacyCrawler):
    has_manga = True
    can_search = True
    base_url = ["https://mangakatana.com/"]

    def search_novel(self, query):
        soup = self.get_soup(search_url % quote_plus(query))

        results = []
        for item in soup.select("#book_list .item"):
            a = item.select_one("h3.title a")
            if not a or not a.get("href"):
                continue
            latest = item.select_one(".chapter a")
            results.append(
                {
                    "title": a.get_text(strip=True),
                    "url": self.absolute_url(a["href"]),
                    "info": latest.get_text(strip=True) if latest else "",
                }
            )
        return results

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)

        heading = soup.select_one("h1.heading") or soup.select_one("h1")
        self.novel_title = heading.get_text(strip=True) if heading else ""
        logger.info("Novel title: %s", self.novel_title)

        cover = soup.select_one(".cover img")
        if cover:
            self.novel_cover = self.absolute_url(cover.get("src") or cover.get("data-src"))
        logger.info("Novel cover: %s", self.novel_cover)

        authors = [a.get_text(strip=True) for a in soup.select(".author") if a.get_text(strip=True)]
        if authors:
            self.novel_author = ", ".join(dict.fromkeys(authors))

        # Chapters are listed newest-first; reverse to read oldest-first.
        for a in reversed(soup.select(".chapters .chapter a[href]")):
            chap_id = 1 + len(self.chapters)
            vol_id = 1 + (chap_id - 1) // 100
            if len(self.volumes) < vol_id:
                self.volumes.append(Volume(id=vol_id))
            self.chapters.append(
                Chapter(
                    id=chap_id,
                    volume=vol_id,
                    title=a.get_text(strip=True),
                    url=self.absolute_url(a["href"]),
                )
            )

    def download_chapter_body(self, chapter):
        # Page image URLs live in a `var thzq = [...]` array in the chapter HTML.
        html = self.get_response(chapter["url"]).text
        match = re.search(r"var\s+thzq\s*=\s*\[([\s\S]*?)\]", html)
        if not match:
            return ""
        urls = [u for u in re.findall(r"'([^']+)'", match.group(1)) if u.startswith("http")]
        return "<p>" + "</p><p>".join(f'<img src="{u}">' for u in urls) + "</p>"
