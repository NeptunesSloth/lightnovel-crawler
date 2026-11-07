import logging
import re
from typing import Any, Dict

from lncrawl.core import Chapter, LegacyCrawler, Volume
from lncrawl.core.models import SearchResult
from lncrawl.utils.novel_status import from_page

logger = logging.getLogger(__name__)

_PAGE_PARAM = re.compile(r"page=(\d+)")


class NovelFireCrawler(LegacyCrawler):
    search_url = "https://novelfire.net/search?keyword={}&type=title"
    base_url = [
        "https://novelfire.net/",
    ]
    has_mtl = False
    has_manga = False
    can_search = True

    def initialize(self) -> None:
        self.init_executor(workers=self.workers, ratelimit=3)

    def read_novel_info(self) -> None:
        soup = self.get_soup(self.novel_url)

        self.novel_title = soup.find("h1").text.strip()
        self.novel_author = soup.select_one('span[itemprop="author"]').text.strip()

        img = soup.select_one(".cover img")
        self.novel_cover = self.absolute_url(img["src"])

        summary = soup.select_one(".summary .content")
        if summary:
            self.novel_synopsis = self.cleaner.extract_contents(summary)

        self.novel_tags = [a.text.strip() for a in soup.select(".categories a") if a.text.strip()]
        # "<strong>Ongoing</strong> <small>Status</small>" in the header stats
        self.novel_status = from_page(soup)

        # The chapter list is paged, each page a volume. The first page's
        # pagination names later pages, so those are fetched side by side; the
        # "next" link from the last one fetched then carries on, in case the
        # pagination left pages out.
        first_url = self.novel_url + "/chapters"
        pages: Dict[int, Any] = {1: self.get_soup(first_url)}
        fetched = {self.absolute_url(first_url)}
        while True:
            last = max(pages)
            next_a = pages[last].select_one("a.page-link[rel='next']")
            if not next_a:
                break
            next_url = self.absolute_url(next_a["href"])
            if next_url in fetched:
                break  # a "next" link back to a page already read
            wanted = {last + 1: next_url}
            if _PAGE_PARAM.search(next_url):
                numbers = [
                    int(match.group(1))
                    for a in pages[last].select("a.page-link[href]")
                    for match in [_PAGE_PARAM.search(a["href"])]
                    if match
                ]
                for number in range(last + 2, max(numbers, default=0) + 1):
                    wanted[number] = _PAGE_PARAM.sub(f"page={number}", next_url)
            futures = {
                number: self.submit_task(self.get_soup, url) for number, url in wanted.items()
            }
            for number, future in futures.items():
                pages[number] = future.result()
            fetched.update(wanted.values())

        for vol_id, number in enumerate(sorted(pages), start=1):
            for a in pages[number].select("ul.chapter-list li a"):
                self.chapters.append(
                    Chapter(
                        id=len(self.chapters) + 1,
                        volume=vol_id,
                        title=a["title"],
                        url=self.absolute_url(a["href"]),
                    )
                )
            self.volumes.append(Volume(id=vol_id))

    def download_chapter_body(self, chapter: Chapter) -> str:
        soup = self.get_soup(chapter["url"])
        contents = soup.select_one("div#content")
        if not contents:
            return ""

        # Only remove standalone titles. A wrapper's combined text can start
        # with "Chapter N" while containing the entire chapter underneath it.
        heading_tags = ["h1", "h2", "h3", "h4", "h5", "h6"]
        elements_to_check = contents.find_all([*heading_tags, "p"], limit=5)
        chapter_title = re.sub(r"\W+", " ", chapter.title).strip().casefold()
        for element in elements_to_check:
            if element.find(["div", "p", *heading_tags]):
                continue
            element_text = element.get_text(" ", strip=True)
            if not element_text:
                continue
            is_title_heading = element.name in heading_tags and re.match(
                r"(?i)^\s*chapter\s+\d+\b", element_text
            )
            is_exact_title = (
                chapter_title
                and re.sub(r"\W+", " ", element_text).strip().casefold() == chapter_title
            )
            if is_title_heading or is_exact_title:
                element.decompose()

        return self.cleaner.extract_contents(contents)

    def search_novel(self, query):
        query = query.lower().replace(" ", "+")
        soup = self.get_soup(self.search_url.format(query))

        results = []
        for item in soup.select("ul.novel-list li.novel-item"):
            a = item.select_one("a")

            title = a.get("title")
            url = self.absolute_url(a.get("href"))

            chapters = a.select_one(".icon-book-open").text
            rank = a.select_one(".icon-crown").text
            results.append(
                SearchResult(
                    title=title,
                    url=url,
                    info=f"{chapters} | {rank}",
                )
            )

        return results
