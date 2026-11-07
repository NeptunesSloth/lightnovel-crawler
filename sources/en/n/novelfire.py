import logging
import re
import threading
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

from lncrawl.core import Chapter, LegacyCrawler, Volume
from lncrawl.core.models import SearchResult
from lncrawl.utils.chapter_content import has_chapter_content
from lncrawl.utils.novel_status import from_page

logger = logging.getLogger(__name__)

_PAGE_PARAM = re.compile(r"page=(\d+)")
_HTML_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"

# A chapter's address mapped onto the mirror: (prefix it starts with, prefix on
# the mirror, query the mirror adds).
_Mapping = Tuple[str, str, str]


def _mapping(url: str, final_url: str) -> Optional[_Mapping]:
    """How ``url`` turned into ``final_url``, as prefixes around a shared path tail."""
    source, final = urlsplit(url), urlsplit(final_url)
    if not source.netloc or not final.netloc or source.query or source.fragment:
        return None
    own = [part for part in source.path.split("/") if part]
    theirs = [part for part in final.path.split("/") if part]
    shared = 0
    while shared < min(len(own), len(theirs)) and own[-1 - shared] == theirs[-1 - shared]:
        shared += 1
    if shared == 0:
        return None  # nothing of the chapter's own address survives: no rule to learn
    keep = len(own) - shared
    lead = len(theirs) - shared
    source_prefix = f"{source.scheme}://{source.netloc}/" + "".join(f"{p}/" for p in own[:keep])
    final_prefix = f"{final.scheme}://{final.netloc}/" + "".join(f"{p}/" for p in theirs[:lead])
    if (source_prefix, final.query) == (final_prefix, ""):
        return None  # served from where it was asked: nothing to skip
    return source_prefix, final_prefix, final.query


class _MirrorMap:
    """Where NovelFire's chapter redirects end, learned while downloading.

    Trusted only once two chapters in a row mapped the same way, so an address
    carrying something chapter-specific (a signed token) is never guessed; and
    given up for the rest of the run after a few direct requests that did not
    return the story.
    """

    GIVE_UP_AFTER = 3

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen: Optional[_Mapping] = None
        self._trusted: Optional[_Mapping] = None
        self._misses = 0
        self._off = False

    def direct_url(self, url: str) -> Optional[str]:
        with self._lock:
            rule = self._trusted
        if not rule or not url.startswith(rule[0]) or "?" in url or "#" in url:
            return None
        direct = rule[1] + url[len(rule[0]) :]
        return f"{direct}?{rule[2]}" if rule[2] else direct

    def learn(self, url: str, final_url: str) -> None:
        if url == final_url:
            return
        rule = _mapping(url, final_url)
        with self._lock:
            if self._off or not rule:
                return
            if self._trusted is None and rule == self._seen:
                self._trusted = rule
            self._seen = rule

    def hit(self) -> None:
        with self._lock:
            self._misses = 0

    def miss(self) -> None:
        with self._lock:
            self._misses += 1
            if self._misses >= self.GIVE_UP_AFTER:
                self._trusted = None
                self._off = True


# One per process: the mapping is the site's, whichever crawler instance learns it.
_MIRRORS = _MirrorMap()


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
        # NovelFire answers each chapter with a redirect to a "Loading..." page
        # whose script sends the reader on to a mirror (novelphoenix.com) that
        # holds the text: three requests a chapter, each waiting its turn with
        # the site. Once two chapters have shown where the chain ends, ask the
        # mirror straight away. A page without the story falls back to the chain.
        mirror = _MIRRORS.direct_url(chapter["url"])
        if mirror:
            body = self._chapter_body(self._fetch_page(mirror)[0], chapter)
            if has_chapter_content(body):
                _MIRRORS.hit()
                return body
            _MIRRORS.miss()

        soup, final_url = self._fetch_page(chapter["url"])
        _MIRRORS.learn(chapter["url"], final_url)
        return self._chapter_body(soup, chapter)

    def _fetch_page(self, url: str) -> Tuple[Any, str]:
        """The page at ``url`` and the address it was finally served from."""
        response = self.scraper.fetch("GET", url, headers={"accept": _HTML_ACCEPT})
        return self.scraper.make_soup(response), str(response.url or url)

    def _chapter_body(self, soup: Any, chapter: Chapter) -> str:
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
