"""Filling a novel's missing chapters ("Fill missing chapters").

In order, stopping as soon as nothing is missing:

1. a copy of the same title already in the library (no download);
2. the novel's own site, asked again for just the missing chapters;
3. other sites that have the same title: their chapter lists are read and the
   chapters whose names match a missing one are downloaded straight into this
   novel.

Nothing else is added to the library. It used to need another copy of the
novel there: a novel on its own had "No other copy", and the search of other
sites worked by adding each site's whole novel as a second library entry.
"""

import re
from typing import Dict, List

import sqlmodel as sq

from ....context import ctx
from ....core import Chapter as CrawlerChapter, Novel as CrawlerNovel
from ....dao import Chapter
from ....enums import JobType
from ....utils.chapter_content import has_chapter_content
from ....utils.time_utils import current_timestamp
from ._base import AbortedException, BaseHandler, HandlerException

# how many other sources to try searching, and how many matching sources to
# actually download from, per run — keeps a heal from turning into a crawl-storm
MAX_SEARCH_SOURCES = 12
MAX_MATCH_SOURCES = 2


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


class HealNovelHandler(BaseHandler):
    @staticmethod
    def can_activate(job) -> bool:
        return job.type == JobType.HEAL_NOVEL

    def _set_progress(self, done: int, total: int) -> None:
        with ctx.db.session() as sess:
            ctx.jobs._update(sess, self.job.id, done=done, total=max(total, 1))
            sess.commit()
        self.job.done = done
        self.job.total = max(total, 1)

    def run(self) -> None:
        novel_id = self.job.extra.get("novel_id")
        if not novel_id:
            raise HandlerException("No novel id")
        novel = ctx.novels.get(novel_id)
        self._set_running()

        books = getattr(ctx, "books", None)
        if books is not None:
            books.require(novel_id)  # an unplugged drive is not "everything missing"

        def missing_chapters():
            # one listing of the folder, not a read of every chapter file
            rows = ctx.chapters.list(novel_id=novel_id)
            have = ctx.files.chapters_with_content(novel_id, [row.serial for row in rows])
            return [row for row in rows if row.serial not in have]

        missing = missing_chapters()
        total_missing = len(missing)
        if not total_missing:
            self._set_extra(healed=0, message="Nothing missing — already complete.")
            return
        self._set_progress(0, total_missing)

        # 1) free wins first: copy from same-title copies already in the library
        local = ctx.novels.heal_from_library(novel_id)
        healed = int(local.get("healed") or 0)
        self._set_progress(min(healed, total_missing), total_missing)
        missing = missing_chapters()
        if not missing:
            self._set_extra(
                healed=healed,
                message=f"Filled all {healed} missing chapter(s) from your other copies.",
            )
            return

        # 2) the novel's own site, for just the chapters that are missing
        self._set_extra(phase="downloading", source=novel.domain)
        own = 0
        try:
            crawler = ctx.sources.init_crawler(novel.url)
        except Exception as e:
            ctx.logger.debug(f"Fill missing: no crawler for {novel.url}: {e}")
            crawler = None
        if crawler is not None:
            try:
                for chapter in missing:
                    if self.signal.is_set():
                        raise AbortedException()
                    try:
                        ctx.crawler.fetch_chapter(
                            self.user.id, chapter.id, signal=self.signal, custom=crawler
                        )
                        own += 1
                        self._set_progress(min(healed + own, total_missing), total_missing)
                    except AbortedException:
                        raise
                    except Exception as e:
                        ctx.logger.debug(f"Fill missing: {novel.domain} chapter failed: {e}")
            finally:
                crawler.close()
        healed += own
        missing = missing_chapters()
        if not missing:
            self._set_extra(
                phase="done",
                healed=healed,
                message=f"Filled all {healed} missing chapter(s) ({own} from {novel.domain}).",
            )
            return

        # 3) other sites with the same title: read their chapter lists and
        # download the chapters named like a missing one straight into this novel
        wanted = _norm(novel.title)
        by_title: Dict[str, Chapter] = {_norm(c.title): c for c in missing if c.title}
        tried = 0
        matched = 0
        elsewhere = 0
        for src in ctx.sources.list(can_search=True):
            if self.signal.is_set():
                raise AbortedException()
            if src.domain == novel.domain:
                continue
            if tried >= MAX_SEARCH_SOURCES or matched >= MAX_MATCH_SOURCES:
                break
            if not by_title:
                break
            tried += 1
            self._set_extra(phase="searching", source=src.domain)
            try:
                results = ctx.crawler.search_novel(
                    user_id=self.user.id,
                    query=novel.title,
                    domain=src.domain,
                    signal=self.signal,
                )
            except AbortedException:
                raise
            except Exception as e:
                ctx.logger.debug(f"Fill missing: search failed on {src.domain}: {e}")
                continue
            hit = next((r for r in results or [] if _norm(r.title) == wanted), None)
            if not hit:
                continue
            matched += 1

            self._set_extra(phase="downloading", source=src.domain)
            try:
                got = self._from_other_site(hit.url, by_title)
            except AbortedException:
                raise
            except Exception as e:
                ctx.logger.debug(f"Fill missing: {src.domain} failed: {e}")
                continue
            ctx.logger.info(f"Fill missing: {len(got)} chapter(s) from {src.domain}")
            for title in got:
                by_title.pop(title, None)
            elsewhere += len(got)
            healed += len(got)
            self._set_progress(min(healed, total_missing), total_missing)

        if healed and not by_title and not missing_chapters():
            message = f"Filled all {healed} missing chapter(s)."
        elif healed:
            left = len(missing_chapters())
            message = (
                f"Filled {healed} of {total_missing} missing chapter(s)"
                + (
                    f" ({own} from {novel.domain}, {elsewhere} from other sites)"
                    if elsewhere
                    else ""
                )
                + (f"; {left} could not be found anywhere." if left else ".")
            )
        elif matched:
            message = (
                f"{novel.domain} would not give the missing chapters, and other sites that "
                "have this novel name their chapters differently."
            )
        elif tried:
            message = (
                f"{novel.domain} would not give the missing chapters, and no other "
                "searchable site has this novel."
            )
        else:
            message = f"{novel.domain} would not give the missing chapters."
        self._set_extra(phase="done", healed=healed, message=message)

    def _from_other_site(self, url: str, by_title: Dict[str, Chapter]) -> List[str]:
        """Download the chapters ``by_title`` names from the novel at ``url``; the titles got.

        Text chapters only: a manga chapter's pages belong to the site they
        came from. Nothing of the other site's novel is kept in the library.
        """
        got: List[str] = []
        with ctx.crawler.prepare_crawler(self.user.id, url, self.signal) as crawler:
            try:
                listing = CrawlerNovel(url=url)
                crawler.read_novel(listing)
                crawler.format_novel(listing)
                for theirs in listing.chapters or []:
                    if self.signal.is_set():
                        raise AbortedException()
                    title = _norm(theirs.title or "")
                    target = by_title.get(title)
                    if target is None or title in got:
                        continue
                    model = CrawlerChapter(url=theirs.url, id=target.serial, title=theirs.title)
                    try:
                        crawler.download_chapter(model)
                        crawler.format_chapter(model)
                    except AbortedException:
                        raise
                    except Exception as e:
                        ctx.logger.debug(f"Fill missing: chapter {theirs.url} failed: {e}")
                        continue
                    if not model.body or not has_chapter_content(model.body) or model.images:
                        continue
                    ctx.files.save_text(target.content_file, model.body)
                    extra = dict(**target.extra)
                    extra.pop("download_status", None)
                    extra.pop("download_error", None)
                    extra["filled_from"] = theirs.url
                    with ctx.db.session() as sess:
                        sess.exec(
                            sq.update(Chapter)
                            .where(sq.col(Chapter.id) == target.id)
                            .values(is_done=True, extra=extra, updated_at=current_timestamp())
                        )
                        sess.commit()
                    got.append(title)
            finally:
                crawler.close()
        return got
