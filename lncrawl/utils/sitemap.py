"""Sitemap mining — enumerate a source's catalogue when its search cannot.

A crawler only exposes ``search(query)``, and for nearly every source that maps
to a *single page* of the site's own search results (15-30 items). Merging a few
dozen seed searches therefore tops out at a few dozen novels even on a site that
hosts tens of thousands — which is why a "download everything" export used to
come back as a zip with ~30 books in it.

Almost all of those sites publish an XML sitemap (declared in ``robots.txt`` or
sitting at a conventional path), and it lists every novel page. These helpers
walk it — sitemap indexes, gzipped documents and all — and keep the URLs that
look like novel pages. What a novel page "looks like" is learned from the URLs
the source's own search returned, so no per-source configuration is needed.
"""

import gzip
import html
import logging
import re
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import urljoin, urlparse

from .text_tools import format_title
from .url_tools import extract_host

logger = logging.getLogger(__name__)

# The shape of a novel URL: (parent path, number of path segments). Novel pages
# on a source all share one — "book/some-slug" is ("book", 2) — while chapter
# pages ("book/some-slug/chapter-1") are deeper and category pages sit
# elsewhere, so this alone separates novels from the rest of a sitemap.
UrlShape = Tuple[str, int]

# Fetches a URL and returns its raw bytes, or None if it could not be read.
Fetch = Callable[[str], Optional[bytes]]

# Tried in order when robots.txt declares no sitemap of its own.
DEFAULT_SITEMAP_PATHS: Tuple[str, ...] = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/sitemap.xml.gz",
    "/wp-sitemap.xml",
    "/sitemap/sitemap-index.xml",
    "/sitemap/novel.xml",
    "/novel-sitemap.xml",
    "/book-sitemap.xml",
    "/sitemap-novels.xml",
)

# Top-level slugs that are never a novel. Only consulted for sources whose novel
# URLs sit at the root of the site (e.g. "https://site.com/some-novel"), where
# the shape alone would also match every static page.
_NON_NOVEL_SLUGS: Set[str] = {
    "about",
    "account",
    "advanced-search",
    "author",
    "authors",
    "blog",
    "bookmark",
    "bookmarks",
    "browse",
    "category",
    "chapter",
    "chapters",
    "completed",
    "contact",
    "cookie-policy",
    "dmca",
    "faq",
    "feed",
    "genre",
    "genres",
    "history",
    "home",
    "hot",
    "index",
    "latest",
    "library",
    "list",
    "login",
    "logout",
    "most-popular",
    "new",
    "news",
    "novel-list",
    "page",
    "popular",
    "privacy",
    "privacy-policy",
    "profile",
    "ranking",
    "rankings",
    "register",
    "rss",
    "search",
    "series",
    "sitemap",
    "tag",
    "tags",
    "terms",
    "terms-of-service",
    "top",
    "trending",
    "updates",
    "user",
}

_LOC_RE = re.compile(
    rb"<loc>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</loc>",
    re.IGNORECASE | re.DOTALL,
)
_INDEX_RE = re.compile(rb"<sitemapindex", re.IGNORECASE)
_ROBOTS_SITEMAP_RE = re.compile(r"^\s*sitemap\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE)
_PAGE_SUFFIX_RE = re.compile(r"\.(html?|php|aspx?|jsp)$", re.IGNORECASE)
_GZIP_MAGIC = b"\x1f\x8b"


def decode_document(data: bytes) -> bytes:
    """Gunzip a sitemap body when it is compressed (``.xml.gz`` or gzip-encoded)."""
    if data[:2] == _GZIP_MAGIC:
        try:
            return gzip.decompress(data)
        except OSError:
            return data
    return data


def url_shape(url: str) -> Optional[UrlShape]:
    """The (parent path, depth) shape of a URL, or None if it has no path."""
    segments: List[str] = [s for s in urlparse(url).path.split("/") if s]
    if not segments:
        return None
    return ("/".join(segments[:-1]).lower(), len(segments))


def learn_shapes(urls: Iterable[str]) -> Set[UrlShape]:
    """Learn what a novel URL looks like on this source from known novel URLs.

    Shapes seen more than once win; a source that returned only a couple of
    results contributes all of them rather than nothing.
    """
    counts: Dict[UrlShape, int] = {}
    for url in urls:
        shape = url_shape(url)
        if shape:
            counts[shape] = counts.get(shape, 0) + 1
    if not counts:
        return set()
    repeated = {shape for shape, count in counts.items() if count > 1}
    return repeated or set(counts)


def title_from_url(url: str) -> str:
    """A readable title guessed from a novel URL's slug (sitemaps carry no titles)."""
    segments: List[str] = [s for s in urlparse(url).path.split("/") if s]
    if not segments:
        return ""
    slug = _PAGE_SUFFIX_RE.sub("", segments[-1])
    slug = re.sub(r"[-_+]+", " ", slug).strip()
    return format_title(slug)


def _is_plausible_novel_slug(url: str) -> bool:
    """Filter for root-level novel URLs, where the shape matches static pages too."""
    segments: List[str] = [s for s in urlparse(url).path.split("/") if s]
    if not segments:
        return False
    slug = _PAGE_SUFFIX_RE.sub("", segments[-1]).lower()
    if slug in _NON_NOVEL_SLUGS:
        return False
    # novel slugs are multi-word ("the-beginning-after-the-end"); single-word
    # root pages are almost always navigation
    return len(slug) >= 8 and ("-" in slug or "_" in slug)


def matches_shape(url: str, shapes: Set[UrlShape], hosts: Set[str]) -> bool:
    """Whether a sitemap URL looks like a novel page on this source."""
    if not url.startswith("http"):
        return False
    if hosts and extract_host(url) not in hosts:
        return False
    shape = url_shape(url)
    if shape is None or shape not in shapes:
        return False
    if shape[0] == "" and shape[1] == 1 and not _is_plausible_novel_slug(url):
        return False
    return True


def sitemaps_from_robots(text: str, base_url: str) -> List[str]:
    """The sitemap URLs a robots.txt declares."""
    found: List[str] = []
    for match in _ROBOTS_SITEMAP_RE.findall(text or ""):
        url = urljoin(base_url, html.unescape(match.strip()))
        if url.startswith("http") and url not in found:
            found.append(url)
    return found


def _clean_loc(raw: bytes) -> str:
    return html.unescape(raw.decode("utf-8", errors="replace").strip()).rstrip("/")


def collect_sitemap_urls(
    fetch: Fetch,
    base_urls: Sequence[str],
    shapes: Set[UrlShape],
    should_stop: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[int, int, int], None]] = None,
    max_documents: int = 300,
    max_urls: int = 50000,
    hosts: Optional[Set[str]] = None,
) -> List[str]:
    """Walk a site's sitemaps and return every URL that looks like a novel page.

    ``fetch`` returns the raw bytes of a URL (None when it fails), which lets the
    caller reuse the crawler's session, pacing and Cloudflare clearance.
    ``should_stop`` is polled between documents so an aborted or time-capped
    discovery returns what it has instead of grinding on. ``on_progress`` is
    called as ``(documents_done, documents_known, urls_found)``. ``hosts`` limits
    which hosts a URL may live on; it defaults to the hosts of ``base_urls``, and
    a source with mirror domains should pass all of them.
    """
    if not shapes:
        return []

    allowed: Set[str] = set(hosts or {extract_host(url) for url in base_urls if url})
    allowed.discard("")

    queue: List[str] = []
    queued: Set[str] = set()

    def enqueue(url: str) -> None:
        url = url.strip()
        if not url.startswith("http") or url in queued:
            return
        if allowed and extract_host(url) not in allowed:
            return
        queued.add(url)
        queue.append(url)

    for base_url in base_urls:
        if should_stop is not None and should_stop():
            break
        robots = fetch(urljoin(base_url, "/robots.txt"))
        if robots:
            for url in sitemaps_from_robots(robots.decode("utf-8", errors="replace"), base_url):
                enqueue(url)
    if not queue:
        # robots.txt declared none — probe the conventional locations instead
        for base_url in base_urls:
            for path in DEFAULT_SITEMAP_PATHS:
                enqueue(urljoin(base_url, path))

    found: List[str] = []
    seen: Set[str] = set()
    documents = 0
    while queue and documents < max_documents and len(found) < max_urls:
        if should_stop is not None and should_stop():
            break
        url = queue.pop(0)
        documents += 1
        data = fetch(url)
        if not data:
            continue
        data = decode_document(data)
        locations: List[bytes] = _LOC_RE.findall(data)
        if not locations:
            continue
        is_index = bool(_INDEX_RE.search(data))
        for raw in locations:
            location = _clean_loc(raw)
            if not location:
                continue
            if is_index:
                enqueue(location)
            elif location not in seen and matches_shape(location, shapes, allowed):
                seen.add(location)
                found.append(location)
                if len(found) >= max_urls:
                    logger.info(f"Sitemap cap of {max_urls} novel URLs reached")
                    break
        if on_progress is not None:
            on_progress(documents, documents + len(queue), len(found))

    logger.info(f"Sitemap walk read {documents} document(s) and found {len(found)} novel URL(s)")
    return found
