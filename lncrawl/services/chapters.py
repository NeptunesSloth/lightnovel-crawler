from typing import Any, Dict, List, Optional, Tuple

import sqlmodel as sq

from ..context import ctx
from ..core import Chapter as CrawlerChapter, PageSoup
from ..dao import Chapter, Job, LanguageCode, User, Volume
from ..dao.chapter import ChapterTranslation
from ..exceptions import ServerErrors
from ..server.models import Paginated, ReadChapterResponse
from ..server.models.novel import ChapterGap, NovelCompleteness


class ChapterService:
    def __init__(self) -> None:
        pass

    def completeness(self, novel_id: str) -> NovelCompleteness:
        """Inspect actual cached content in one chapter query, not completion flags.

        The files are looked at with one listing of the novel's folder (and the
        checks kept beside them), not a stat and a read of each: opening a
        5,000-chapter novel read 5,000 files off the drive every time.

        Only the serials are read for every chapter; whole rows only for the
        gaps. Making a row object of each chapter took most of the time on a
        novel of 11,000 chapters, all of them downloaded.
        """
        books = getattr(ctx, "books", None)
        if books is not None:
            books.require(novel_id)  # unplugged is not "nothing downloaded"
        with ctx.db.session() as sess:
            serials = sess.exec(
                sq.select(Chapter.serial)
                .where(Chapter.novel_id == novel_id)
                .order_by(sq.col(Chapter.serial).asc())
            ).all()
        have = ctx.files.chapters_with_content(novel_id, serials)
        self._mark_downloaded(novel_id, have)
        novels = getattr(ctx, "novels", None)
        skipped = (set(novels.skipped_serials(novel_id)) if novels is not None else set()) - set(
            have
        )
        gaps: List[ChapterGap] = []
        lacking = [s for s in serials if s not in have and s not in skipped]
        for chapter in self._by_serials(novel_id, lacking):
            status = "missing"
            if (
                ctx.files.exists(chapter.content_file)
                or chapter.extra.get("download_status") == "empty"
            ):
                status = "empty"
            elif chapter.extra.get("download_status") == "failed":
                status = "failed"
            gaps.append(
                ChapterGap(
                    id=chapter.id,
                    serial=chapter.serial,
                    title=chapter.title,
                    status=status,
                    error=chapter.extra.get("download_error"),
                )
            )
        return NovelCompleteness(
            novel_id=novel_id,
            total=len(serials),
            downloaded=len(serials) - len(gaps) - len(skipped & set(serials)),
            missing=sum(gap.status == "missing" for gap in gaps),
            failed=sum(gap.status == "failed" for gap in gaps),
            empty=sum(gap.status == "empty" for gap in gaps),
            skipped=len(skipped & set(serials)),
            complete=bool(serials) and not gaps,
            gaps=gaps,
        )

    def _mark_downloaded(self, novel_id: str, have: Any) -> None:
        """Say so in the database for chapters whose text is here but not marked.

        The library's "1957/1958 downloaded" counts the marks, the novel's page
        counts the files: a chapter saved just as the app closed (or put back
        from a copy of the database) left the two a chapter apart for good.
        """
        if not have:
            return
        try:
            with ctx.db.session() as sess:
                unmarked = sess.exec(
                    sq.select(Chapter.serial).where(
                        Chapter.novel_id == novel_id, sq.col(Chapter.is_done).is_(False)
                    )
                ).all()
                stale = [serial for serial in unmarked if serial in have]
                for start in range(0, len(stale), 500):
                    sess.exec(
                        sq.update(Chapter)  # type: ignore[call-overload]
                        .where(sq.col(Chapter.novel_id) == novel_id)
                        .where(sq.col(Chapter.serial).in_(stale[start : start + 500]))
                        .values(is_done=True)
                    )
                if stale:
                    sess.commit()
        except Exception:
            pass  # a count that stays off by one is not worth failing the page for

    def _by_serials(self, novel_id: str, serials: List[int]) -> List[Chapter]:
        """These chapters of a novel, in serial order (asked a few hundred at a time)."""
        found: List[Chapter] = []
        with ctx.db.session() as sess:
            for start in range(0, len(serials), 500):
                found.extend(
                    sess.exec(
                        sq.select(Chapter).where(
                            Chapter.novel_id == novel_id,
                            sq.col(Chapter.serial).in_(serials[start : start + 500]),
                        )
                    ).all()
                )
        found.sort(key=lambda chapter: chapter.serial)
        return found

    def list(
        self,
        *,
        novel_id: Optional[str] = None,
        volume_id: Optional[str] = None,
        is_crawled: Optional[bool] = None,
        language: Optional[LanguageCode] = None,
    ) -> List[Chapter]:
        with ctx.db.session() as sess:
            stmt = sq.select(Chapter)
            if novel_id:
                stmt = stmt.where(Chapter.novel_id == novel_id)
            if volume_id:
                stmt = stmt.where(Chapter.volume_id == volume_id)
            if is_crawled is not None:
                stmt = stmt.where(sq.col(Chapter.is_done).is_(is_crawled))
            stmt = stmt.order_by(sq.col(Chapter.serial).asc())
            items = list(sess.exec(stmt).all())
        self._put_translation(items, language)
        return items

    def list_page(
        self,
        offset: int = 0,
        limit: int = 20,
        *,
        novel_id: Optional[str] = None,
        volume_id: Optional[str] = None,
        is_crawled: Optional[bool] = None,
        language: Optional[LanguageCode] = None,
    ) -> Paginated[Chapter]:
        with ctx.db.session() as sess:
            stmt = sq.select(Chapter)
            cnt = sq.select(sq.func.count()).select_from(Chapter)

            # Apply filters
            conditions: List[Any] = []
            if novel_id:
                conditions += [Chapter.novel_id == novel_id]
            if volume_id:
                conditions += [Chapter.volume_id == volume_id]
            if is_crawled is not None:
                conditions += [sq.col(Chapter.is_done).is_(is_crawled)]

            if conditions:
                cnt = cnt.where(*conditions)
                stmt = stmt.where(*conditions)

            # Apply sorting
            stmt = stmt.order_by(sq.col(Chapter.serial).asc())

            # Apply pagination
            stmt = stmt.offset(offset).limit(limit)

            total = sess.exec(cnt).one()
            items = list(sess.exec(stmt).all())

            # get translations
            self._put_translation(items, language)

            return Paginated(
                total=total,
                offset=offset,
                limit=limit,
                items=list(items),
            )

    def list_ids(
        self,
        novel_id: Optional[str] = None,
        volume_id: Optional[str] = None,
        is_crawled: Optional[bool] = None,
        descending: bool = False,
        limit: Optional[int] = None,
    ) -> List[str]:
        with ctx.db.session() as sess:
            stmt = sq.select(Chapter.id)
            if novel_id:
                stmt = stmt.where(Chapter.novel_id == novel_id)
            if volume_id:
                stmt = stmt.where(Chapter.volume_id == volume_id)
            if is_crawled is not None:
                stmt = stmt.where(sq.col(Chapter.is_done).is_(is_crawled))
            if descending:
                stmt = stmt.order_by(sq.col(Chapter.serial).desc())
            else:
                stmt = stmt.order_by(sq.col(Chapter.serial).asc())
            if limit:
                stmt = stmt.limit(limit)
            items = sess.exec(stmt).all()
            return list(items)

    def find(self, novel_id: str, serial: int) -> Chapter:
        with ctx.db.session() as sess:
            stmt = sq.select(Chapter).where(
                Chapter.novel_id == novel_id,
                Chapter.serial == serial,
            )
            chapter = sess.exec(stmt).first()
            if not chapter:
                raise ServerErrors.no_such_chapter
            return chapter

    def get(self, chapter_id: str) -> Chapter:
        with ctx.db.session() as sess:
            chapter = sess.get(Chapter, chapter_id)
            if not chapter:
                raise ServerErrors.no_such_chapter
            return chapter

    def counts_by_url(self, urls: List[str]) -> Dict[str, Tuple[int, int]]:
        """(chapters, chapters downloaded) of each novel at these addresses in the library.

        By the chapters' own marks, without reading their files: quick enough for
        a whole site's novels, and close enough to show how far a download is.
        """
        from ..dao import Novel

        result: Dict[str, Tuple[int, int]] = {}
        unique = list(dict.fromkeys(urls))
        with ctx.db.session() as sess:
            for offset in range(0, len(unique), 500):
                batch = unique[offset : offset + 500]
                ids = dict(
                    sess.exec(
                        sq.select(Novel.id, Novel.url).where(sq.col(Novel.url).in_(batch))
                    ).all()
                )
                if not ids:
                    continue
                rows = sess.exec(
                    sq.select(
                        Chapter.novel_id,
                        sq.func.count(),
                        sq.func.sum(sq.case((sq.col(Chapter.is_done), 1), else_=0)),
                    )
                    .where(sq.col(Chapter.novel_id).in_(list(ids)))
                    .group_by(Chapter.novel_id)
                ).all()
                for novel_id, total, done in rows:
                    result[ids[novel_id]] = (int(total or 0), int(done or 0))
        return result

    def get_many(self, chapter_ids: List[str]) -> List[Chapter]:
        with ctx.db.session() as sess:
            items = sess.exec(sq.select(Chapter).where(sq.col(Chapter.id).in_(chapter_ids))).all()
            return list(items)

    def delete(self, chapter_id: str) -> None:
        with ctx.db.session() as sess:
            chapter = sess.get(Chapter, chapter_id)
            if not chapter:
                return
            ctx.files.resolve(chapter.content_file).unlink(True)
            sess.delete(chapter)
            sess.commit()

    def _put_translation(
        self,
        items: List[Chapter],
        language: Optional[LanguageCode],
    ):
        if language and items:
            novel_id = items[0].novel_id
            serials = [item.serial for item in items]
            with ctx.db.session() as sess:
                translations = sess.exec(
                    sq.select(ChapterTranslation).where(
                        ChapterTranslation.novel_id == novel_id,
                        ChapterTranslation.language == language,
                        sq.col(ChapterTranslation.chapter_serial).in_(serials),
                    )
                ).all()
                serial_title_map = {t.chapter_serial: t.chapter_title for t in translations}
            for item in items:
                item.title = serial_title_map.get(item.serial) or item.title

    def get_chapter_translation(self, chapter: Chapter, language: LanguageCode):
        with ctx.db.session() as sess:
            return sess.exec(
                sq.select(ChapterTranslation)
                .where(
                    ChapterTranslation.novel_id == chapter.novel_id,
                    ChapterTranslation.chapter_serial == chapter.serial,
                    ChapterTranslation.language == language,
                )
                .limit(1)
            ).first()

    def read(
        self,
        user: User,
        chapter_id: str,
        *,
        auto_fetch: Optional[bool] = None,
        language: Optional[LanguageCode] = None,
        add_to_history: bool = True,
    ) -> ReadChapterResponse:
        if auto_fetch is None:
            auto_fetch = ctx.tier.auto_fetch_enabled(user)

        chapter = self.get(chapter_id)
        if add_to_history:
            ctx.history.add(user.id, chapter.id, chapter)

        job: Optional[Job] = None
        content: Optional[str] = None
        if chapter.is_available:
            content = ctx.files.load_text(chapter.content_file)
        elif auto_fetch:
            job = ctx.jobs.get_chapter_job(user.id, chapter_id)
            if not job:
                job = ctx.jobs.fetch_chapter(user, chapter_id)

        if language:
            content = None
            chapter_translation = self.get_chapter_translation(chapter, language)
            if chapter_translation and chapter_translation.is_available:
                chapter.title = chapter_translation.chapter_title or chapter.title
                content = ctx.files.load_text(chapter_translation.content_file)
            elif auto_fetch:
                fetch_job_id = job.id if job else None
                translate_job = ctx.jobs.get_chapter_translation_job(user.id, chapter_id, language)
                if not translate_job:
                    translate_job = ctx.jobs.translate_chapter(
                        user, chapter_id, language, depends_on=fetch_job_id
                    )
                if not job or job.is_done:
                    job = translate_job

        word_count: Optional[int] = None
        if content:
            word_count = PageSoup.create(content).word_count()

        novel = ctx.novels.get(chapter.novel_id, language)
        with ctx.db.session() as sess:
            previous_id = sess.exec(
                sq.select(Chapter.id)
                .where(Chapter.novel_id == novel.id)
                .where(Chapter.serial == (chapter.serial - 1))
                .limit(1)
            ).first()
            next_id = sess.exec(
                sq.select(Chapter.id)
                .where(Chapter.novel_id == novel.id)
                .where(Chapter.serial == (chapter.serial + 1))
                .limit(1)
            ).first()

        return ReadChapterResponse(
            job=job,
            novel=novel,
            chapter=chapter,
            content=content,
            language=language,
            word_count=word_count,
            next_id=next_id,
            previous_id=previous_id,
        )

    def sync(self, novel_id: str, chapters: List[CrawlerChapter]):
        with ctx.db.session() as sess:
            vol_id_map: Dict[Optional[int], str] = {
                v.serial: v.id
                for v in sess.exec(sq.select(Volume).where(Volume.novel_id == novel_id)).all()
            }

            wanted = {c.id: c for c in chapters}
            existing = {
                c.serial: c
                for c in sess.exec(sq.select(Chapter).where(Chapter.novel_id == novel_id)).all()
            }

            wk = set(wanted.keys())
            ek = set(existing.keys())
            to_insert = wk - ek
            # A chapter that is downloaded is never removed for being missing
            # from the list: a site's chapter page that came back empty or cut
            # short (blocked, rate limited, changed) took every chapter of the
            # novel with it, files and all, the moment the novel was opened.
            # Only chapters never downloaded go, and none when the list is empty.
            gone = ek - wk if wanted else set()
            # ...nor one whose file is there though its row does not say so (the
            # app stopped between the two, or the database was put back from a copy)
            kept = ctx.files.chapters_with_content(novel_id, gone) if gone else set()
            to_delete = {s for s in gone if not existing[s].is_done and s not in kept}
            # Only rows that differ are written. Every chapter used to be given a
            # new time on every re-read of the novel, so each looked newer than
            # the novel's e-book (rebuilt before every library ZIP) and as if it
            # had just arrived.
            to_update = {
                s
                for s in ek & wk
                if existing[s].url != wanted[s].url
                or existing[s].title != wanted[s].title
                or existing[s].volume_id != vol_id_map.get(wanted[s].volume)
                or any(existing[s].extra.get(k) != v for k, v in wanted[s].get_extras().items())
            }

            if to_insert:
                sess.exec(
                    sq.insert(Chapter),
                    params=[
                        Chapter(
                            serial=s,
                            novel_id=novel_id,
                            url=wanted[s].url,
                            title=wanted[s].title,
                            extra=wanted[s].get_extras(),
                            volume_id=vol_id_map.get(wanted[s].volume),
                        ).model_dump(exclude={"is_available", "content_file"})
                        for s in to_insert
                    ],
                )

            if to_update:
                sess.exec(
                    sq.update(Chapter),
                    params=[
                        Chapter(
                            id=existing[s].id,
                            created_at=existing[s].created_at,
                            serial=s,
                            novel_id=novel_id,
                            url=wanted[s].url,
                            title=wanted[s].title,
                            is_done=existing[s].is_done,
                            extra={**existing[s].extra, **wanted[s].get_extras()},
                            volume_id=vol_id_map.get(wanted[s].volume),
                        ).model_dump(exclude={"is_available", "content_file"})
                        for s in to_update
                    ],
                )

            if to_delete:
                sess.exec(
                    sq.delete(Chapter)
                    .where(sq.col(Chapter.novel_id) == novel_id)
                    .where(sq.col(Chapter.serial).in_(to_delete))
                )
                for serial in to_delete:
                    file = existing[serial].content_file
                    ctx.files.resolve(file).unlink(True)

            sess.commit()
