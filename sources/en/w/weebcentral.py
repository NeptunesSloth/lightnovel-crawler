# -*- coding: utf-8 -*-
"""Crawler for https://weebcentral.com/ — a manga reader. Pages are emitted as
<img> tags so the engine downloads and embeds them."""

import logging
import re
from urllib.parse import quote_plus

from lncrawl.core import Chapter, LegacyCrawler, Volume

logger = logging.getLogger(__name__)

ULID = r"[0-9A-Z]{26}"
search_url = (
    "https://weebcentral.com/search/data?text=%s"
    "&sort=Best+Match&order=Descending&official=Any&display_mode=Full+Display&limit=30"
)


class WeebCentralCrawler(LegacyCrawler):
    has_manga = True
    can_search = True
    base_url = ["https://weebcentral.com/"]

    def search_novel(self, query):
        soup = self.get_soup(search_url % quote_plus(query))

        results = []
        seen = set()
        for a in soup.select("a[href*='/series/']"):
            href = a["href"].split("?")[0]
            if not re.search(r"/series/" + ULID, href) or href in seen:
                continue
            seen.add(href)
            title_el = a.select_one(".line-clamp-1")
            img = a.select_one("img")
            title = (
                (title_el.get_text(strip=True) if title_el else "")
                or (img.get("alt", "").replace(" cover", "").strip() if img else "")
                or a.get_text(" ", strip=True)
            )
            results.append({"title": title, "url": href})
        return results

    def read_novel_info(self):
        soup = self.get_soup(self.novel_url)

        heading = soup.select_one("h1")
        self.novel_title = heading.get_text(strip=True) if heading else ""
        logger.info("Novel title: %s", self.novel_title)

        og = soup.select_one("meta[property='og:image']")
        if og and og.get("content"):
            self.novel_cover = og["content"]
        logger.info("Novel cover: %s", self.novel_cover)

        authors = [
            a.get_text(strip=True)
            for a in soup.select("a[href*='author=']")
            if a.get_text(strip=True)
        ]
        if authors:
            self.novel_author = ", ".join(dict.fromkeys(authors))

        # The full chapter list lives behind a dedicated endpoint.
        match = re.search(r"/series/(" + ULID + ")", self.novel_url)
        links = []
        if match:
            chapter_list = self.get_soup(
                f"https://weebcentral.com/series/{match.group(1)}/full-chapter-list"
            )
            links = [
                a
                for a in chapter_list.select("a[href]")
                if re.search(r"/chapters/" + ULID, a.get("href", ""))
            ]

        # Chapters are listed newest-first; reverse to read oldest-first.
        for a in reversed(links):
            title = a.get_text(" ", strip=True).split("Last Read")[0].strip()
            chap_id = 1 + len(self.chapters)
            vol_id = 1 + (chap_id - 1) // 100
            if len(self.volumes) < vol_id:
                self.volumes.append(Volume(id=vol_id))
            self.chapters.append(
                Chapter(
                    id=chap_id,
                    volume=vol_id,
                    title=title or f"Chapter {chap_id}",
                    url=self.absolute_url(a["href"].split("?")[0]),
                )
            )

    def download_chapter_body(self, chapter):
        match = re.search(r"/chapters/(" + ULID + ")", chapter["url"])
        if not match:
            return ""
        url = (
            f"https://weebcentral.com/chapters/{match.group(1)}/images"
            "?is_prev=False&current_page=1&reading_style=long_strip"
        )
        soup = self.get_soup(url)
        urls = [i["src"] for i in soup.select("img[src]") if i.get("src", "").startswith("http")]
        return "<p>" + "</p><p>".join(f'<img src="{u}">' for u in urls) + "</p>"
