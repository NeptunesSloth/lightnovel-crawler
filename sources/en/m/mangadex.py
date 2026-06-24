# -*- coding: utf-8 -*-
"""Crawler for https://mangadex.org/ — built on the public MangaDex JSON API
(https://api.mangadex.org). Manga pages are emitted as <img> tags so the engine
downloads and embeds them like any other manga source."""

import logging
import re
from urllib.parse import quote_plus

from lncrawl.core import Chapter, LegacyCrawler, Volume

logger = logging.getLogger(__name__)

API = "https://api.mangadex.org"
UPLOADS = "https://uploads.mangadex.org"
UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
LANG = "en"
RATINGS = "contentRating[]=safe&contentRating[]=suggestive&contentRating[]=erotica&contentRating[]=pornographic"
# MangaDex's API rejects browser User-Agents (HTTP 400); send a plain client UA.
API_HEADERS = {"User-Agent": "lightnovel-crawler (+https://github.com/dipu-bd/lightnovel-crawler)"}


def _pick_title(attributes: dict) -> str:
    title = attributes.get("title") or {}
    if title.get("en"):
        return title["en"]
    for alt in attributes.get("altTitles") or []:
        if alt.get("en"):
            return alt["en"]
    if title:
        return next(iter(title.values()))
    return ""


class MangaDexCrawler(LegacyCrawler):
    has_manga = True
    can_search = True
    base_url = ["https://mangadex.org/"]

    def initialize(self) -> None:
        # MangaDex enforces a global rate limit; keep requests modest
        self.init_executor(ratelimit=3)

    def search_novel(self, query):
        url = f"{API}/manga?title={quote_plus(query)}&limit=20&{RATINGS}&includes[]=cover_art"
        data = self.get_json(url, headers=API_HEADERS)

        results = []
        for manga in data.get("data", []):
            attrs = manga.get("attributes", {})
            title = _pick_title(attrs)
            if not title:
                continue
            year = attrs.get("year") or ""
            status = attrs.get("status") or ""
            info = " | ".join(str(x) for x in [year, status] if x)
            results.append(
                {
                    "title": title,
                    "url": f"https://mangadex.org/title/{manga['id']}",
                    "info": info,
                }
            )
        return results

    def read_novel_info(self):
        match = UUID_RE.search(self.novel_url)
        if not match:
            raise LookupError("Could not find a MangaDex manga id in the URL")
        manga_id = match.group(1)

        data = self.get_json(
            f"{API}/manga/{manga_id}?includes[]=cover_art&includes[]=author",
            headers=API_HEADERS,
        ).get("data", {})
        attrs = data.get("attributes", {})

        self.novel_title = _pick_title(attrs) or manga_id
        logger.info("Novel title: %s", self.novel_title)

        for rel in data.get("relationships", []):
            rel_attrs = rel.get("attributes") or {}
            if rel.get("type") == "cover_art" and rel_attrs.get("fileName"):
                self.novel_cover = f"{UPLOADS}/covers/{manga_id}/{rel_attrs['fileName']}"
            elif rel.get("type") == "author" and rel_attrs.get("name") and not self.novel_author:
                self.novel_author = rel_attrs["name"]
        logger.info("Novel cover: %s", self.novel_cover)

        synopsis = (attrs.get("description") or {}).get("en")
        if synopsis:
            self.novel_synopsis = synopsis

        # Fetch the chapter list (paginated), keeping one entry per chapter number
        seen = set()
        volume_map: dict = {}
        offset = 0
        while True:
            feed = self.get_json(
                f"{API}/manga/{manga_id}/feed?translatedLanguage[]={LANG}"
                f"&order[volume]=asc&order[chapter]=asc&limit=500&offset={offset}&{RATINGS}",
                headers=API_HEADERS,
            )
            chapters = feed.get("data", [])
            for ch in chapters:
                a = ch.get("attributes", {})
                number = a.get("chapter")
                key = number if number is not None else ch["id"]
                if key in seen:
                    continue  # skip duplicate uploads of the same chapter
                seen.add(key)

                vol = a.get("volume")
                if vol not in volume_map:
                    volume_map[vol] = len(volume_map) + 1
                    self.volumes.append(Volume(id=volume_map[vol]))

                label = f"Chapter {number}" if number is not None else "Oneshot"
                if a.get("title"):
                    label += f": {a['title']}"

                self.chapters.append(
                    Chapter(
                        id=1 + len(self.chapters),
                        volume=volume_map[vol],
                        title=label,
                        url=f"https://mangadex.org/chapter/{ch['id']}",
                    )
                )

            total = feed.get("total", 0)
            offset += 500
            if offset >= total or not chapters:
                break

        logger.info("%d chapters found", len(self.chapters))

    def download_chapter_body(self, chapter):
        match = UUID_RE.search(chapter["url"])
        if not match:
            return ""
        data = self.get_json(f"{API}/at-home/server/{match.group(1)}", headers=API_HEADERS)
        base = (data.get("baseUrl") or "").rstrip("/")
        info = data.get("chapter") or {}
        digest = info.get("hash")
        files = info.get("data") or info.get("dataSaver") or []
        if not base or not digest or not files:
            return ""
        images = [f'<img src="{base}/data/{digest}/{name}">' for name in files]
        return "<p>" + "</p><p>".join(images) + "</p>"
