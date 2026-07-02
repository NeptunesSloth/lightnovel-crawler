"""Cross-source gap download ("deep heal").

When a novel's remaining chapters are unreachable on its own source, this job
searches *other* sources for the same title, downloads only the chapters that
are missing, and copies them into the target via the safe title-matched healing
in ``ctx.novels.heal_from_library``. The extra copy stays in the library (the
export de-dupe already skips same-title copies), so future heals are free.
"""

import re

from ....context import ctx
from ....enums import JobType
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

        def missing_chapters():
            return [c for c in ctx.chapters.list(novel_id=novel_id) if not c.is_available]

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

        # 2) search other sources for the same title and pull just the gaps
        wanted = _norm(novel.title)
        missing_titles = {_norm(c.title) for c in missing if c.title}
        tried = 0
        matched = 0
        for src in ctx.sources.list(can_search=True):
            if self.signal.is_set():
                raise AbortedException()
            if src.domain == novel.domain:
                continue
            if tried >= MAX_SEARCH_SOURCES or matched >= MAX_MATCH_SOURCES:
                break
            if not missing_titles:
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
                ctx.logger.debug(f"Deep heal: search failed on {src.domain}: {e}")
                continue
            hit = next((r for r in results or [] if _norm(r.title) == wanted), None)
            if not hit:
                continue
            matched += 1

            self._set_extra(phase="downloading", source=src.domain)
            try:
                crawler = ctx.sources.init_crawler(hit.url)
            except Exception as e:
                ctx.logger.debug(f"Deep heal: no crawler for {hit.url}: {e}")
                continue
            try:
                copy = ctx.crawler.fetch_novel(
                    self.user.id, hit.url, signal=self.signal, custom=crawler
                )
                fetched = 0
                for ch in ctx.chapters.list(novel_id=copy.id):
                    if self.signal.is_set():
                        raise AbortedException()
                    if ch.is_available or _norm(ch.title) not in missing_titles:
                        continue
                    try:
                        ctx.crawler.fetch_chapter(self.user.id, ch.id, custom=crawler)
                        fetched += 1
                    except Exception as e:
                        ctx.logger.debug(f"Deep heal: chapter fetch failed: {e}")
                ctx.logger.info(f"Deep heal: fetched {fetched} chapter(s) from {src.domain}")
            except AbortedException:
                raise
            except Exception as e:
                ctx.logger.debug(f"Deep heal: fetch failed on {src.domain}: {e}")
                continue
            finally:
                crawler.close()

            # copy whatever was fetched into the target, then re-evaluate the gaps
            res = ctx.novels.heal_from_library(novel_id)
            healed += int(res.get("healed") or 0)
            missing = missing_chapters()
            missing_titles = {_norm(c.title) for c in missing if c.title}
            self._set_progress(min(healed, total_missing), total_missing)

        if healed:
            message = f"Filled {healed} of {total_missing} missing chapter(s) from other sites."
        elif matched:
            message = "Found the novel on other sites but their chapter names didn't match."
        elif tried:
            message = "Couldn't find this novel on other searchable sites."
        else:
            message = "No other searchable sites to try."
        self._set_extra(phase="done", healed=healed, message=message)
