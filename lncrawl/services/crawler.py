from contextlib import contextmanager
from difflib import SequenceMatcher
import logging
from threading import Event
from time import monotonic, sleep
from typing import Callable, Dict, List, Optional, Set, Union

from pydantic import HttpUrl

from ..context import ctx
from ..core import Chapter as CrawlerChapter, Crawler, Novel as CrawlerNovel, SearchResult
from ..dao import Chapter, ChapterImage, Novel
from ..exceptions import ServerErrors
from ..utils.sitemap import collect_sitemap_urls, learn_shapes, title_from_url
from ..utils.url_tools import extract_host

logger = logging.getLogger(__name__)


class CrawlerService:
    def __init__(self) -> None:
        pass

    @contextmanager
    def prepare_crawler(
        self,
        user_id: str,
        url: str,
        signal: Optional[Event] = None,
        custom_crawler: Optional[Crawler] = None,
    ):
        crawler = custom_crawler
        if crawler is None:
            crawler = ctx.sources.init_crawler(url)

        prev_signal = crawler.scraper.signal
        if signal:
            crawler.scraper.signal = signal

        try:
            # login
            can_login = getattr(crawler, "can_login", False)
            logged_in = getattr(crawler, "__logged_in__", False)
            if can_login and not logged_in:
                login = ctx.secrets.get_login(user_id, url)
                if login:
                    crawler.login(login.username, login.password)
                    setattr(crawler, "__logged_in__", True)

            yield crawler

        finally:
            crawler.scraper.signal = prev_signal
            if custom_crawler is None:
                crawler.close()

    def fetch_novel(
        self,
        user_id: str,
        url: Union[str, HttpUrl],
        signal: Optional[Event] = None,
        custom: Optional[Crawler] = None,
    ) -> Novel:
        # validate url
        if isinstance(url, str):
            url = HttpUrl(url)
        if not url.host:
            raise ServerErrors.invalid_url.with_extra(url)
        novel_url = str(url)

        with self.prepare_crawler(user_id, novel_url, signal, custom) as crawler:
            # fetch novel metadata
            model = CrawlerNovel(url=novel_url)
            crawler.read_novel(model)
            if not model.title:
                raise ServerErrors.no_novel_title
            crawler.format_novel(model)
            assert model.volumes is not None
            assert model.chapters is not None

            # get or create novel object
            novel = ctx.novels.find_by_url(novel_url)
            if not novel:
                with ctx.db.session() as sess:
                    novel = Novel(
                        url=novel_url,
                        title=model.title,
                        cover_url=model.cover_url,
                        domain=extract_host(novel_url),
                    )
                    sess.add(novel)
                    sess.commit()

            # update novel info
            novel.title = model.title
            novel.authors = model.author
            novel.cover_url = model.cover_url
            novel.domain = extract_host(novel_url)
            novel.manga = model.is_manga or crawler.has_manga
            novel.mtl = model.is_mtl or crawler.has_mtl
            novel.synopsis = model.synopsis
            novel.tags = model.tags or []
            novel.rtl = model.is_rtl or False
            novel.language = model.language
            novel.volume_count = len(model.volumes)
            novel.chapter_count = len(model.chapters)

            # update novel extra
            extra = dict(**novel.extra)
            extra.update(model.get_extras())
            extra["crawler_version"] = crawler.version
            novel.extra = extra

            # save updates
            with ctx.db.session() as sess:
                sess.merge(novel)
                sess.commit()

            # add or update tags
            ctx.tags.insert(novel.tags)

            # add or update volumes
            ctx.volumes.sync(novel.id, model.volumes)

            # add or update chapters
            ctx.chapters.sync(novel.id, model.chapters)

            # download cover
            crawler.download_cover(
                novel.cover_url or "",
                ctx.files.resolve(novel.cover_file),
            )

            # update output path time (prevents cleaner to delete it)
            ctx.files.utime(f"novels/{novel.id}")

        logger.debug(
            f"Fetched novel: {novel.title} - {novel.chapter_count} chapters | {novel.volume_count} volumes | {novel.url}"
        )
        return novel

    def fetch_chapter(
        self,
        user_id: str,
        chapter_id: str,
        signal: Optional[Event] = None,
        custom: Optional[Crawler] = None,
        refresh: bool = False,
    ) -> Chapter:
        chapter = ctx.chapters.get(chapter_id)
        novel = ctx.novels.get(chapter.novel_id)
        try:
            url = HttpUrl(chapter.url)
        except Exception:
            raise ServerErrors.invalid_url
        if not url.host:
            raise ServerErrors.invalid_url

        with self.prepare_crawler(user_id, novel.url, signal, custom) as crawler:
            # check if download is necessary
            if (
                not refresh
                and chapter.is_available
                and chapter.extra.get("crawler_version") == crawler.version
            ):
                logger.debug(f"Skipped: {novel.title}] - Chapter {chapter.serial}")
                return chapter

            # get chapter content
            model = CrawlerChapter(
                url=str(url),
                id=chapter.serial,
                title=chapter.title,
            )
            model.update(chapter.extra)
            crawler.download_chapter(model)
            crawler.format_chapter(model)
            assert model.body is not None

            # save chapter content
            ctx.files.save_text(chapter.content_file, model.body)

            # save chapter images
            ctx.images.sync(chapter, model.images)

            # set extras
            extra = dict(**chapter.extra)
            extra.update(model.get_extras())
            extra["crawler_version"] = crawler.version
            chapter.extra = extra

            # update title and status
            chapter.is_done = True
            chapter.title = model.title

            # update db
            with ctx.db.session() as sess:
                sess.merge(chapter)
                sess.commit()

            logger.debug(f"Downloaded chapter: {novel.title}] - Chapter {chapter.serial}")
            return chapter

    def fetch_image(
        self,
        user_id: str,
        image_id: str,
        signal: Optional[Event] = None,
        custom: Optional[Crawler] = None,
        refresh: bool = False,
    ) -> ChapterImage:
        image = ctx.images.get(image_id)
        novel = ctx.novels.get(image.novel_id)
        try:
            url = HttpUrl(image.url)
        except Exception:
            raise ServerErrors.invalid_url
        if not url.host:
            raise ServerErrors.invalid_url

        with self.prepare_crawler(user_id, novel.url, signal, custom) as crawler:
            # check if download is necessary
            if (
                not refresh
                and image.is_available
                and image.extra.get("crawler_version") == crawler.version
            ):
                logger.debug(f"Skipped: {novel.title}] - Image {image.id}")
                return image

            # download image
            file = ctx.files.resolve(image.image_file)
            crawler.download_image(str(url), file)

            image.is_done = file.is_file()
            extra = dict(**image.extra)
            extra["crawler_version"] = crawler.version
            image.extra = extra

            # update db
            with ctx.db.session() as sess:
                sess.merge(image)
                sess.commit()

            logger.debug(f"Downloaded image: {novel.title}] - Image {image.id}")
            return image

    def search_novel(
        self,
        user_id: str,
        query: str,
        domain: str,
        signal: Optional[Event] = None,
        custom: Optional[Crawler] = None,
    ) -> List[SearchResult]:
        # get crawler
        source = ctx.sources.get_source(domain)
        with self.prepare_crawler(user_id, source.url, signal, custom) as crawler:
            results = list(crawler.search(query))
            results.sort(key=lambda x: -SequenceMatcher(a=x.title, b=query).ratio())
            return list(results)

    # The default seed queries used to enumerate a source. The crawler base only
    # exposes ``search(query)`` (no universal "list every novel"), so discovery
    # runs the source's own search across these seeds and merges the results.
    DISCOVER_SEEDS: str = "abcdefghijklmnopqrstuvwxyz0123456789"

    # A seed search returns one page of the site's results, so on a big source
    # the seeds alone top out at a few dozen novels. Seeds that came back full
    # are widened with these characters ("a" -> "aa", "ab", ...) to dig past that
    # page — the fallback for sources that publish no usable sitemap.
    DISCOVER_EXPAND_CHARS: str = "abcdefghijklmnopqrstuvwxyz"

    # A seed looks truncated when its search returned at least this many results
    # and matched the biggest page the source handed back.
    DISCOVER_PAGE_HINT: int = 5

    # Skip the (slow) seed expansion when the sitemap already found this many.
    DISCOVER_EXPAND_THRESHOLD: int = 50

    # Bounds for the sitemap walk (see utils/sitemap.py).
    DISCOVER_MAX_SITEMAPS: int = 300
    DISCOVER_MAX_URLS: int = 50000

    def discover_search_results(
        self,
        user_id: str,
        domain: str,
        signal: Optional[Event] = None,
        seeds: Optional[str] = None,
        custom: Optional[Crawler] = None,
        on_progress: Optional[Callable[[int, int, int], None]] = None,
        time_budget: Optional[float] = None,
        delay: float = 0,
        use_sitemap: bool = True,
        expand_seeds: bool = True,
    ) -> List[SearchResult]:
        """Discover every novel a source exposes, in up to three passes.

        1. Run the source's own search across the seed queries and merge the
           results, de-duplicating by URL.
        2. Walk the site's sitemaps for every page whose URL has the same shape
           as the novels found in step 1. A search only ever returns one page of
           results, so this is what turns a few dozen novels into the source's
           actual catalogue.
        3. If the sitemap turned up (almost) nothing, widen the seeds that came
           back full ("a" -> "aa", "ab", ...) to page past the search cap.

        Stops early (returning what was found) if ``signal`` is set, so callers
        can abort cleanly. ``on_progress(done, total, found)`` is called as work
        completes; the total grows as later passes queue more work.
        ``time_budget`` (seconds) caps the whole discovery — useful for a
        throttled source that would otherwise grind for hours. ``delay``
        (seconds) paces the requests to avoid tripping a source's anti-bot.
        """
        source = ctx.sources.get_source(domain)
        seed_list: List[str] = list(seeds or self.DISCOVER_SEEDS)
        found: Dict[str, SearchResult] = {}
        start = monotonic()

        # Progress is driven by a moving total: the seeds are known up front, the
        # sitemap documents and expanded seeds are only known once queued.
        done_count = 0
        total_count = len(seed_list)

        def report(step: int = 1, new_work: int = 0, pending_found: int = 0) -> None:
            nonlocal done_count, total_count
            done_count += step
            total_count = max(total_count + new_work, done_count)
            if on_progress is not None:
                # pending_found covers URLs a pass has collected but not merged yet
                on_progress(done_count, total_count, len(found) + pending_found)

        def out_of_time() -> bool:
            return time_budget is not None and monotonic() - start > time_budget

        def stopped() -> bool:
            return (signal is not None and signal.is_set()) or out_of_time()

        def pause() -> bool:
            """Pace requests (polite mode). Returns True if the run was aborted."""
            if delay <= 0:
                return False
            if signal is not None:
                return signal.wait(delay)
            sleep(delay)
            return False

        def remember(item: SearchResult) -> None:
            key = str(item.url or "").rstrip("/")
            if key and key not in found:
                found[key] = item

        with self.prepare_crawler(user_id, source.url, signal, custom) as crawler:

            def run_seeds(queries: List[str]) -> Dict[str, int]:
                """Search each query and merge the hits; returns per-query counts."""
                counts: Dict[str, int] = {}
                for query in queries:
                    if signal is not None and signal.is_set():
                        break
                    if out_of_time():
                        logger.info(
                            f"Discovery time budget reached on {domain}; "
                            f"stopping with {len(found)} found"
                        )
                        break
                    hits = 0
                    try:
                        for item in crawler.search(query):
                            hits += 1
                            remember(item)
                    except Exception as e:
                        logger.debug(f"Discover '{query}' on {domain} failed: {e}")
                    counts[query] = hits
                    report()
                    if pause():
                        break
                return counts

            seed_counts = run_seeds(seed_list)

            sitemap_added = 0
            if use_sitemap and not stopped():
                sitemap_added = self._mine_sitemap(
                    crawler=crawler,
                    domain=domain,
                    origin=source.url,
                    found=found,
                    remember=remember,
                    report=report,
                    stopped=stopped,
                    pause=pause,
                )

            # Only worth paging past the search cap by hand when the sitemap
            # didn't already hand us the catalogue — this pass is slow.
            if expand_seeds and sitemap_added < self.DISCOVER_EXPAND_THRESHOLD and not stopped():
                page_size = max(seed_counts.values(), default=0)
                if page_size >= self.DISCOVER_PAGE_HINT:
                    truncated = [q for q, hits in seed_counts.items() if hits >= page_size]
                    expanded = [q + c for q in truncated for c in self.DISCOVER_EXPAND_CHARS]
                    logger.info(
                        f"Search on {domain} caps at {page_size} results per query; "
                        f"widening {len(truncated)} seed(s) into {len(expanded)} searches"
                    )
                    report(step=0, new_work=len(expanded))
                    run_seeds(expanded)

        return list(found.values())

    def _mine_sitemap(
        self,
        crawler: Crawler,
        domain: str,
        origin: str,
        found: Dict[str, SearchResult],
        remember: Callable[[SearchResult], None],
        report: Callable[..., None],
        stopped: Callable[[], bool],
        pause: Callable[[], bool],
    ) -> int:
        """Add every novel page the source's sitemap lists. Returns how many are new.

        The URLs already in ``found`` are the template: only sitemap entries with
        the same URL shape (same parent path, same depth) are taken, which keeps
        chapter, genre and static pages out.
        """
        shapes = learn_shapes(found.keys())
        if not shapes:
            logger.info(f"No novel URL pattern learned on {domain}; skipping the sitemap")
            return 0

        def fetch(url: str) -> Optional[bytes]:
            if stopped():
                return None
            try:
                response = crawler.scraper.get(url)
                if int(getattr(response, "status_code", 200) or 200) >= 400:
                    return None
                data: bytes = response.content
            except Exception as e:
                logger.debug(f"Sitemap fetch failed on {url}: {e}")
                return None
            pause()
            return data

        def on_document(documents: int, known: int, urls: int) -> None:
            # fold the still-queued documents into the progress total, and show
            # the URLs mined so far even though they are merged only at the end
            report(step=0, new_work=max(0, known - documents), pending_found=urls)
            report(pending_found=urls)

        mirrors = crawler.base_url if isinstance(crawler.base_url, list) else [crawler.base_url]
        hosts: Set[str] = {extract_host(url) for url in [origin, *mirrors] if url}
        hosts.discard("")

        urls = collect_sitemap_urls(
            fetch,
            [origin],
            shapes,
            should_stop=stopped,
            on_progress=on_document,
            max_documents=self.DISCOVER_MAX_SITEMAPS,
            max_urls=self.DISCOVER_MAX_URLS,
            hosts=hosts,
        )

        before = len(found)
        for url in urls:
            remember(SearchResult(title=title_from_url(url), url=url))
        added = len(found) - before
        logger.info(f"Sitemap discovery on {domain} added {added} novel(s)")
        return added

    def discover_novels(
        self,
        user_id: str,
        domain: str,
        signal: Optional[Event] = None,
        seeds: Optional[str] = None,
        custom: Optional[Crawler] = None,
        on_progress: Optional[Callable[[int, int, int], None]] = None,
        time_budget: Optional[float] = None,
        delay: float = 0,
        use_sitemap: bool = True,
        expand_seeds: bool = True,
    ) -> List[str]:
        """Discover every novel URL a source exposes (see discover_search_results)."""
        results = self.discover_search_results(
            user_id,
            domain,
            signal,
            seeds,
            custom,
            on_progress,
            time_budget,
            delay,
            use_sitemap,
            expand_seeds,
        )
        return [item.url for item in results]
