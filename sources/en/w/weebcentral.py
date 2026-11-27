# -*- coding: utf-8 -*-
"""Crawler for https://weebcentral.com/ — a manga reader. Pages are emitted as
<img> tags so the engine downloads and embeds them."""

import logging
import re
from urllib.parse import quote_plus

from lncrawl.core import Chapter, LegacyCrawler, Volume
from lncrawl.utils.novel_status import from_page

logger = logging.getLogger(__name__)

ULID = r"[0-9A-Z]{26}"
# Series looked at for a ranking at most (the site lists about this many).
_POPULAR_MOST = 40000
search_url = (
    "https://weebcentral.com/search/data?text=%s"
    "&sort=Best+Match&order=Descending&official=Any&display_mode=Full+Display&limit=30"
)


class WeebCentralCrawler(LegacyCrawler):
    has_manga = True
    can_search = True
    can_rank = True
    base_url = ["https://weebcentral.com/"]

    def popular(self):
        """Every series, most popular first, from the site's own search sorted that way."""
        seen = set()
        for offset in range(0, _POPULAR_MOST, 32):
            soup = self.get_soup(
                "https://weebcentral.com/search/data?limit=32&offset=%d&sort=Popularity"
                "&order=Descending&official=Any&anime=Any&adult=Any&display_mode=Full+Display"
                % offset
            )
            fresh = []
            for a in soup.select("a[href*='/series/']"):
                match = re.search(r"/series/(" + ULID + ")", a["href"])
                if match and match.group(1) not in seen:
                    seen.add(match.group(1))
                    fresh.append(f"https://weebcentral.com/series/{match.group(1)}")
            if not fresh:
                return  # past the last page
            yield from fresh

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

        # The info list links each fact to a search: type (Manga / Manhwa /
        # Manhua) and adult content become tags so the library can filter on
        # them, and the status says whether the series is finished.
        tags = [
            a.get_text(strip=True)
            for a in soup.select("a[href*='included_type='], a[href*='included_tag=']")
            if a.get_text(strip=True)
        ]
        if soup.select_one("a[href*='adult=True']"):
            tags.append("Adult")
        self.novel_tags = list(dict.fromkeys(tags))
        # the series description sits in the paragraph after its heading
        for heading in soup.select("strong"):
            if heading.get_text(strip=True) == "Description":
                paragraph = heading.find_next("p")
                if paragraph:
                    self.novel_synopsis = paragraph.get_text(strip=True)
                break
        status = soup.select_one("a[href*='included_status=']")
        self.novel_status = from_page(soup) or (status.get_text(strip=True) if status else None)

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
