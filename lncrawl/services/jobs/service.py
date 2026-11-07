from __future__ import annotations

from typing import Any, Callable, Iterable, List, Optional, Tuple, TypeVar, Union

from sqlalchemy.orm import aliased
import sqlmodel as sq
from sqlmodel import Session

from ...context import ctx
from ...dao import (
    ActivityType,
    Job,
    JobPriority,
    JobStatus,
    JobType,
    LanguageCode,
    OutputFormat,
    User,
    UserRole,
)
from ...exceptions import ServerErrors
from ...server.models import Paginated
from ...utils.time_utils import current_timestamp
from .utils import select_ancestors, select_descendants

T = TypeVar("T")

job_status_type = Job.__table__.c.status.type  # type: ignore
job_failed_literal = sq.cast(sq.literal(JobStatus.FAILED.name), job_status_type)
job_success_literal = sq.cast(sq.literal(JobStatus.SUCCESS.name), job_status_type)
job_running_literal = sq.cast(sq.literal(JobStatus.RUNNING.name), job_status_type)
job_canceled_literal = sq.cast(sq.literal(JobStatus.CANCELED.name), job_status_type)
job_pending_literal = sq.cast(sq.literal(JobStatus.PENDING.name), job_status_type)


def max_parallel_exports() -> int:
    """How many sites may bulk-download at once: all but two of the workers."""
    return max(1, int(ctx.config.crawler.runner_concurrency) - 2)


class JobService:
    # -------------------------------------------------------------------------
    #                               GET Jobs
    # -------------------------------------------------------------------------
    def list(
        self,
        offset: int = 0,
        limit: int = 20,
        *,
        user_id: Optional[str] = None,
        job_type: Optional[JobType] = None,
        priority: Optional[JobPriority] = None,
        status: Optional[JobStatus] = None,
        is_done: Optional[bool] = None,
        parent_job_id: Optional[str] = None,
    ) -> Paginated[Job]:
        with ctx.db.session() as sess:
            stmt = sq.select(Job)
            cnt = sq.select(sq.func.count()).select_from(Job)

            # Apply filters
            conditions: List[Any] = []
            if user_id is not None:
                conditions.append(Job.user_id == user_id)
            if job_type is not None:
                conditions.append(Job.type == job_type)
            if status is not None:
                conditions.append(Job.status == status)
            if is_done is not None:
                conditions.append(sq.col(Job.is_done).is_(True))
            if priority is not None:
                conditions.append(Job.priority == priority)
            if parent_job_id is not None:
                conditions.append(Job.parent_job_id == parent_job_id)
            else:
                conditions.append(
                    sq.or_(
                        Job.status == JobStatus.PAUSED,
                        sq.col(Job.parent_job_id).is_(None),
                    )
                )

            if conditions:
                stmt = stmt.where(*conditions)
                cnt = cnt.where(*conditions)

            # Apply sorting
            if parent_job_id is not None:
                stmt = stmt.order_by(sq.asc(Job.created_at))
            else:
                stmt = stmt.order_by(sq.desc(Job.created_at))

            # Apply pagination
            stmt = stmt.offset(offset).limit(limit)

            total = sess.exec(cnt).one()
            items = sess.exec(stmt).all()

            return Paginated(
                total=total,
                offset=offset,
                limit=limit,
                items=list(items),
            )

    def count(self) -> int:
        with ctx.db.session() as sess:
            return sess.scalar(sq.select(sq.func.count()).select_from(Job)) or 0

    def get(self, job_id: str) -> Job:
        with ctx.db.session() as sess:
            job = sess.get(Job, job_id)
            if not job:
                raise ServerErrors.no_such_job
            return job

    def get_user_id(self, job_id: str) -> Optional[str]:
        with ctx.db.session() as sess:
            stmt = sq.select(Job.user_id).where(Job.id == job_id)
            return sess.exec(stmt).first()

    def verify_access(self, user: User, job_id: str) -> str:
        user_id = self.get_user_id(job_id)
        if not user_id:
            raise ServerErrors.no_such_job
        if user_id != user.id and user.role != UserRole.ADMIN:
            raise ServerErrors.forbidden
        return user_id

    def get_children_ids(self, parent_job_id: str) -> List[str]:
        with ctx.db.session() as sess:
            stmt = sq.select(Job.id).where(Job.parent_job_id == parent_job_id)
            return list(sess.exec(stmt).all())

    def get_children(self, parent_job_id: str) -> List[Job]:
        with ctx.db.session() as sess:
            stmt = sq.select(Job).where(Job.parent_job_id == parent_job_id)
            return list(sess.exec(stmt).all())

    def get_chapter_job(self, user_id: str, chapter_id: str) -> Optional[Job]:
        with ctx.db.session() as sess:
            return sess.exec(
                sq.select(Job)
                .where(
                    Job.user_id == user_id,
                    Job.type == JobType.CHAPTER,
                    sq.col(Job.is_done).is_(False),
                    sq.col(Job.status).in_(
                        [JobStatus.PENDING, JobStatus.RUNNING, JobStatus.PAUSED]
                    ),
                    sq.col(Job.parent_job_id).is_(None),
                    Job.extra["chapter_id"].as_string() == chapter_id,
                )
                .limit(1)
            ).first()

    def get_chapter_translation_job(
        self, user_id: str, chapter_id: str, language: LanguageCode
    ) -> Optional[Job]:
        with ctx.db.session() as sess:
            return sess.exec(
                sq.select(Job)
                .where(
                    Job.user_id == user_id,
                    Job.type == JobType.CHAPTER_TRANSLATION,
                    sq.col(Job.is_done).is_(False),
                    sq.col(Job.status).in_(
                        [JobStatus.PENDING, JobStatus.RUNNING, JobStatus.PAUSED]
                    ),
                    sq.col(Job.parent_job_id).is_(None),
                    Job.extra["chapter_id"].as_string() == chapter_id,
                    Job.extra["language"].as_string() == language,
                )
                .limit(1)
            ).first()

    def get_root(self, job_id: str) -> Optional[Job]:
        with ctx.db.session() as sess:
            return sess.scalar(
                sq.select(Job)
                .where(sq.col(Job.parent_job_id).is_(None))
                .where(sq.col(Job.id).in_(select_ancestors(job_id)))
                .limit(1)
            )

    # -------------------------------------------------------------------------
    #                              CREATE Jobs
    # -------------------------------------------------------------------------
    def fetch_novel(
        self,
        user: User,
        url: str,
        *,
        full: bool = False,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        **data: Any,
    ) -> Job:
        domain = ctx.sources.get_domain(url)
        ctx.sources.get_source(domain)  # validate
        data.update({"url": url})
        novel = ctx.novels.find_by_url(url)
        if novel:
            data["novel_id"] = novel.id
        return self._create(
            user=user,
            data=data,
            parent_id=parent_id,
            depends_on=depends_on,
            type=JobType.FULL_NOVEL if full else JobType.NOVEL,
        )

    def fetch_many_novels(
        self,
        user: User,
        *urls: str,
        full: bool = False,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        **data: Any,
    ) -> Job:
        data.update({"urls": list(set(urls))})
        return self._create(
            user=user,
            data=data,
            parent_id=parent_id,
            depends_on=depends_on,
            type=JobType.FULL_NOVEL_BATCH if full else JobType.NOVEL_BATCH,
        )

    def fetch_volume(
        self,
        user: User,
        volume_id: str,
        *,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        **data: Any,
    ) -> Job:
        volume = ctx.volumes.get(volume_id)
        data.update(
            {
                "volume_id": volume_id,
                "volume_serial": volume.serial,
            }
        )
        if not data.get("novel_title"):
            novel = ctx.novels.get(volume.novel_id)
            data.update(
                {
                    "novel_id": novel.id,
                    "novel_title": novel.title,
                }
            )
        return self._create(
            user=user,
            data=data,
            parent_id=parent_id,
            depends_on=depends_on,
            type=JobType.VOLUME,
        )

    def fetch_many_volumes(
        self,
        user: User,
        *volume_ids: str,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        **data: Any,
    ) -> Job:
        data.update(
            {
                "volume_ids": volume_ids,
            }
        )
        return self._create(
            user=user,
            data=data,
            parent_id=parent_id,
            depends_on=depends_on,
            type=JobType.VOLUME_BATCH,
        )

    def fetch_chapter(
        self,
        user: User,
        chapter_id: str,
        *,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        **data: Any,
    ) -> Job:
        chapter = ctx.chapters.get(chapter_id)
        data.setdefault("novel_id", chapter.novel_id)
        data.update(
            {
                "chapter_id": chapter_id,
                "chapter_serial": chapter.serial,
            }
        )
        if not data.get("novel_title"):
            novel = ctx.novels.get(chapter.novel_id)
            data.update(
                {
                    "novel_id": novel.id,
                    "novel_title": novel.title,
                }
            )
        return self._create(
            user=user,
            data=data,
            parent_id=parent_id,
            depends_on=depends_on,
            type=JobType.CHAPTER,
        )

    def fetch_chapters_under(
        self,
        user: User,
        parent_id: str,
        chapter_ids: List[str],
        job_ids: List[str],
        **data: Any,
    ) -> List[Job]:
        """Create one CHAPTER child job of ``parent_id`` per chapter, all at once.

        The same jobs `fetch_chapter` makes one by one, with the ids given, but in
        one transaction: the chapters are read in one query, the jobs written in
        one insert, and the parent's counts raised once.
        """
        if not chapter_ids:
            return []
        chapters = {chapter.id: chapter for chapter in ctx.chapters.get_many(chapter_ids)}
        if chapters and not data.get("novel_title"):
            novel = ctx.novels.get(next(iter(chapters.values())).novel_id)
            data.update({"novel_id": novel.id, "novel_title": novel.title})
        priority = ctx.tier.job_priority(user)
        jobs: List[Job] = []
        for chapter_id, job_id in zip(chapter_ids, job_ids):
            chapter = chapters.get(chapter_id)
            if chapter is None:
                continue
            extra = dict(data)
            extra.setdefault("novel_id", chapter.novel_id)
            extra.update({"chapter_id": chapter_id, "chapter_serial": chapter.serial})
            job = Job(
                type=JobType.CHAPTER,
                extra=extra,
                user_id=user.id,
                parent_job_id=parent_id,
                priority=priority,
            )
            job.id = job_id
            jobs.append(job)
        if not jobs:
            return []
        with ctx.db.session() as sess:
            sess.add_all(jobs)
            sess.flush()
            self._update_up(sess, parent_id, total=Job.total + len(jobs), inclusive=True)
            sess.commit()
        return jobs

    def fetch_many_chapters(
        self,
        user: User,
        *chapter_ids: str,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        **data: Any,
    ) -> Job:
        data.update(
            {
                "chapter_ids": chapter_ids,
            }
        )
        return self._create(
            user=user,
            data=data,
            parent_id=parent_id,
            depends_on=depends_on,
            type=JobType.CHAPTER_BATCH,
        )

    def translate_chapter(
        self,
        user: User,
        chapter_id: str,
        language: LanguageCode,
        *,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        job_id: Optional[str] = None,
        **data: Any,
    ) -> Job:
        chapter = ctx.chapters.get(chapter_id)
        data.update(
            {
                "chapter_id": chapter_id,
                "chapter_serial": chapter.serial,
                "language": language,
            }
        )
        if not data.get("novel_title"):
            novel = ctx.novels.get(chapter.novel_id)
            data.update(
                {
                    "novel_id": novel.id,
                    "novel_title": novel.title,
                }
            )
        return self._create(
            user=user,
            data=data,
            parent_id=parent_id,
            depends_on=depends_on,
            type=JobType.CHAPTER_TRANSLATION,
            job_id=job_id,
        )

    def translate_many_chapters(
        self,
        user: User,
        *chapter_ids: str,
        language: LanguageCode,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        **data: Any,
    ) -> Job:
        data.update(
            {
                "chapter_ids": list(chapter_ids),
                "language": language,
            }
        )
        return self._create(
            user=user,
            data=data,
            parent_id=parent_id,
            depends_on=depends_on,
            type=JobType.CHAPTER_TRANSLATION_BATCH,
        )

    def translate_novel(
        self,
        user: User,
        novel_id: str,
        language: LanguageCode,
        *,
        full: bool = False,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        **data: Any,
    ) -> Job:
        novel = ctx.novels.get(novel_id)
        data.update(
            {
                "novel_id": novel_id,
                "novel_title": novel.title,
                "language": language,
            }
        )
        return self._create(
            user=user,
            data=data,
            parent_id=parent_id,
            depends_on=depends_on,
            type=JobType.FULL_NOVEL_TRANSLATION if full else JobType.NOVEL_TRANSLATION,
        )

    def translate_many_novels(
        self,
        user: User,
        *novel_ids: str,
        language: LanguageCode,
        full: bool = False,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        **data: Any,
    ) -> Job:
        data.update(
            {
                "novel_ids": list(novel_ids),
                "language": language,
            }
        )
        return self._create(
            user=user,
            data=data,
            parent_id=parent_id,
            depends_on=depends_on,
            type=JobType.FULL_NOVEL_TRANSLATION_BATCH if full else JobType.NOVEL_TRANSLATION_BATCH,
        )

    def translate_volume(
        self,
        user: User,
        volume_id: str,
        language: LanguageCode,
        *,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        **data: Any,
    ) -> Job:
        volume = ctx.volumes.get(volume_id)
        data.update(
            {
                "volume_id": volume_id,
                "volume_serial": volume.serial,
                "language": language,
            }
        )
        if not data.get("novel_title"):
            novel = ctx.novels.get(volume.novel_id)
            data.update(
                {
                    "novel_id": novel.id,
                    "novel_title": novel.title,
                }
            )
        return self._create(
            user=user,
            data=data,
            parent_id=parent_id,
            depends_on=depends_on,
            type=JobType.VOLUME_TRANSLATION,
        )

    def translate_many_volumes(
        self,
        user: User,
        *volume_ids: str,
        language: LanguageCode,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        **data: Any,
    ) -> Job:
        data.update(
            {
                "volume_ids": list(volume_ids),
                "language": language,
            }
        )
        return self._create(
            user=user,
            data=data,
            parent_id=parent_id,
            depends_on=depends_on,
            type=JobType.VOLUME_TRANSLATION_BATCH,
        )

    def fetch_image(
        self,
        user: User,
        image_id: str,
        *,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        job_id: Optional[str] = None,
        **data: Any,
    ) -> Job:
        image = ctx.images.get(image_id)
        data.update(
            {
                "image_id": image_id,
                "url": image.url,
            }
        )
        return self._create(
            user=user,
            data=data,
            parent_id=parent_id,
            depends_on=depends_on,
            type=JobType.IMAGE,
            job_id=job_id,
        )

    def fetch_many_images(
        self,
        user: User,
        *image_ids: str,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        **data: Any,
    ) -> Job:
        data.update(
            {
                "image_ids": image_ids,
            }
        )
        return self._create(
            user=user,
            data=data,
            parent_id=parent_id,
            depends_on=depends_on,
            type=JobType.IMAGE_BATCH,
        )

    def make_artifact(
        self,
        user: User,
        novel_id: str,
        format: OutputFormat,
        *,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        language: Optional[LanguageCode] = None,
        **data: Any,
    ) -> Job:
        data.update(
            {
                "novel_id": novel_id,
                "format": format,
                "language": language,
            }
        )
        if not data.get("novel_title"):
            novel = ctx.novels.get(novel_id)
            data.update(
                {
                    "novel_title": novel.title,
                }
            )
        return self._create(
            user=user,
            data=data,
            parent_id=parent_id,
            depends_on=depends_on,
            type=JobType.ARTIFACT,
        )

    def make_many_artifacts(
        self,
        user: User,
        novel_id: str,
        *formats: OutputFormat,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        language: Optional[LanguageCode] = None,
        **data: Any,
    ) -> Job:
        data.update(
            {
                "novel_id": novel_id,
                "formats": formats,
                "language": language,
            }
        )
        if not data.get("novel_title"):
            novel = ctx.novels.get(novel_id)
            data.update(
                {
                    "novel_title": novel.title,
                }
            )
        return self._create(
            user=user,
            data=data,
            parent_id=parent_id,
            depends_on=depends_on,
            type=JobType.ARTIFACT_BATCH,
        )

    def fetch_missing_chapters(
        self,
        user: User,
        novel_id: str,
        *,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        **data: Any,
    ) -> Job:
        novel = ctx.novels.get(novel_id)
        data.update(
            {
                "novel_id": novel_id,
                "novel_title": novel.title,
            }
        )
        return self._create(
            user=user,
            data=data,
            parent_id=parent_id,
            depends_on=depends_on,
            type=JobType.FETCH_MISSING,
        )

    def heal_novel(
        self,
        user: User,
        novel_id: str,
        *,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        **data: Any,
    ) -> Job:
        """Create a job that fills missing chapters from other sites (deep heal)."""
        novel = ctx.novels.get(novel_id)
        data.update(
            {
                "novel_id": novel_id,
                "novel_title": novel.title,
            }
        )
        return self._create(
            user=user,
            data=data,
            parent_id=parent_id,
            depends_on=depends_on,
            type=JobType.HEAL_NOVEL,
        )

    def fetch_latest(
        self,
        user: User,
        novel_id: str,
        *,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        **data: Any,
    ) -> Job:
        novel = ctx.novels.get(novel_id)
        data.update(
            {
                "novel_id": novel_id,
                "novel_title": novel.title,
            }
        )
        return self._create(
            user=user,
            data=data,
            parent_id=parent_id,
            depends_on=depends_on,
            type=JobType.FETCH_LATEST,
        )

    def search_all_sources(
        self,
        user: User,
        query: str,
        *,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        **data: Any,
    ) -> Job:
        data["search_results"] = []
        data["query"] = str(query).strip().casefold()
        return self._create(
            user=user,
            data=data,
            parent_id=parent_id,
            depends_on=depends_on,
            type=JobType.SEARCH_ALL_SOURCES,
        )

    def search_single_source(
        self,
        user: User,
        query: str,
        domain: str,
        *,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        **data: Any,
    ) -> Job:
        data["domain"] = domain
        data["search_results"] = []
        data["query"] = str(query).strip().casefold()
        source = ctx.sources.get_source(domain)
        if not source.can_search:
            raise ServerErrors.search_not_supported.with_extra(domain)
        data["url"] = source.url
        return self._create(
            user=user,
            data=data,
            parent_id=parent_id,
            depends_on=depends_on,
            type=JobType.SEARCH_SOURCE,
        )

    def discover_source(
        self,
        user: User,
        url: str,
        *,
        full: bool = False,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        **data: Any,
    ) -> Job:
        domain = ctx.sources.get_domain(url)
        source = ctx.sources.get_source(domain)
        if not source.can_search:
            raise ServerErrors.search_not_supported.with_extra(domain)
        data["domain"] = source.domain
        data["url"] = source.url
        data["full"] = bool(full)
        return self._create(
            user=user,
            data=data,
            parent_id=parent_id,
            depends_on=depends_on,
            type=JobType.DISCOVER_SOURCE,
        )

    def export_source(
        self,
        user: User,
        url: str,
        *,
        format: OutputFormat = OutputFormat.epub,
        limit: Optional[int] = None,
        retries: int = 3,
        polite: bool = False,
        dedupe: bool = True,
        discovery_minutes: int = 10,
        max_chapters: int = 0,
        resume: bool = True,
        requests_per_sec: float = 0,
        auto_tune: bool = True,
        update_only: bool = False,
        use_sitemap: bool = True,
        urls: Optional[List[str]] = None,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        parallel_novels: int = 2,
        until_done: bool = False,
        library_only: bool = False,
        **data: Any,
    ) -> Job:
        domain = ctx.sources.get_domain(url)
        source = ctx.sources.get_source(domain)
        # finding a site's novels needs its search; downloading listed ones does not
        if not urls and not source.can_search:
            raise ServerErrors.search_not_supported.with_extra(domain)
        data["domain"] = source.domain
        data["url"] = source.url
        data["format"] = OutputFormat(format).value
        data["retries"] = max(0, int(retries))
        data["polite"] = bool(polite)
        data["dedupe"] = bool(dedupe)
        data["discovery_minutes"] = max(1, int(discovery_minutes))
        data["max_chapters"] = max(0, int(max_chapters))
        data["resume"] = bool(resume)
        data["requests_per_sec"] = max(0.0, float(requests_per_sec))
        data["auto_tune"] = bool(auto_tune)
        data["update_only"] = bool(update_only)
        data["use_sitemap"] = bool(use_sitemap)
        data["parallel_novels"] = max(1, min(4, int(parallel_novels)))
        data["until_done"] = bool(until_done)
        data["library_only"] = bool(library_only)
        if limit:
            data["limit"] = int(limit)
        # When specific novels are picked, export exactly those (skip discovery)
        if urls:
            data["urls"] = [str(u) for u in urls]
        return self._create(
            user=user,
            data=data,
            parent_id=parent_id,
            depends_on=depends_on,
            type=JobType.EXPORT_SOURCE,
        )

    def bulk_download(
        self,
        user: User,
        urls: List[str],
        *,
        parent_id: Optional[str] = None,
        **data: Any,
    ) -> List[Job]:
        """Download every chapter of these novels into the library, unattended.

        One "until done" export per site, in library-only mode, so every bulk
        download gets the same engine: one speed limit per site that tunes itself,
        several novels at once, recovery from stalls and blocks, and resume.
        """
        by_site: dict = {}
        for url in dict.fromkeys(str(u) for u in urls if u):
            try:
                site = ctx.sources.get_domain(url)
                ctx.sources.get_source(site)
            except Exception:
                continue  # no source for it here; nothing could download it
            by_site.setdefault(site, []).append(url)
        if not by_site:
            raise ServerErrors.no_novels_to_download
        return [
            self.export_source(
                user,
                site_urls[0],
                urls=site_urls,
                until_done=True,
                library_only=True,
                parent_id=parent_id,
                **data,
            )
            for site_urls in by_site.values()
        ]

    def import_archive(
        self,
        user: User,
        archive_file: str,
        archive_name: str,
        *,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        **data: Any,
    ) -> Job:
        data["archive_file"] = archive_file
        data["archive_name"] = archive_name
        return self._create(
            user=user,
            data=data,
            parent_id=parent_id,
            depends_on=depends_on,
            type=JobType.IMPORT_ARCHIVE,
        )

    def list_source(
        self,
        user: User,
        url: str,
        *,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        **data: Any,
    ) -> Job:
        domain = ctx.sources.get_domain(url)
        source = ctx.sources.get_source(domain)
        if not source.can_search:
            raise ServerErrors.search_not_supported.with_extra(domain)
        data["domain"] = source.domain
        data["url"] = source.url
        return self._create(
            user=user,
            data=data,
            parent_id=parent_id,
            depends_on=depends_on,
            type=JobType.LIST_SOURCE,
        )

    # -------------------------------------------------------------------------
    #                              DELETE Jobs
    # -------------------------------------------------------------------------
    def delete(self, job_id: str) -> None:
        with ctx.db.session() as sess:
            result = sess.exec(
                sq.select(Job.done, Job.total, Job.failed).where(Job.id == job_id)
            ).first()
            if not result:
                return
            done, total, failed = result

            self._update_up(
                sess,
                job_id=job_id,
                done=Job.done - done,
                total=Job.total - total,
                failed=Job.failed - failed,
            )

            sa_deps = select_descendants(job_id, True)
            sess.exec(sq.delete(Job).where(sq.col(Job.id).in_(sa_deps)))

            sess.commit()

    # -------------------------------------------------------------------------
    #                              CANCEL Jobs
    # -------------------------------------------------------------------------

    def requeue_stale_running(self) -> int:
        """Requeue jobs left RUNNING by a previous process (the app closed mid-run).

        Called once at scheduler startup, before any worker is launched, so every
        RUNNING job is necessarily stale — nothing is executing it. Resetting them
        to PENDING lets the runner re-claim and resume them (a source export skips
        already-finished novels via its manifest). Without this, an export orphaned
        by closing the app stays RUNNING forever, displaying its last frozen state.
        Returns the number requeued.
        """
        with ctx.db.session() as sess:
            result = sess.exec(
                sq.update(Job)
                .where(sq.col(Job.status) == JobStatus.RUNNING)
                .values(status=job_pending_literal, updated_at=current_timestamp())
            )
            sess.commit()
            count = getattr(result, "rowcount", 0) or 0
        if count:
            ctx.logger.info(f"Requeued {count} stale running job(s) on startup")
        return count

    # Jobs that put things in the library, shown together on the Downloads page.
    DOWNLOAD_TYPES = (
        JobType.EXPORT_SOURCE,
        JobType.DISCOVER_SOURCE,
        JobType.FULL_NOVEL,
        JobType.FULL_NOVEL_BATCH,
        JobType.NOVEL,
        JobType.NOVEL_BATCH,
        JobType.FETCH_MISSING,
        JobType.FETCH_LATEST,
        JobType.IMPORT_ARCHIVE,
    )
    # Job details too big to send every few seconds: long URL lists and results.
    _BULKY_EXTRA = ("urls", "discovered", "search_results", "novels", "items")

    def list_downloads(self, user: User, recent: int = 25) -> List[dict]:
        """Every download still going (paused and queued included), then recent ones.

        One flat list for the Downloads page: top-level jobs only, newest last
        among the active ones, with bulky details reduced to counts.
        """
        with ctx.db.session() as sess:
            base = sq.select(Job).where(
                sq.col(Job.parent_job_id).is_(None),
                sq.col(Job.type).in_(self.DOWNLOAD_TYPES),
            )
            if user.role not in (UserRole.ADMIN, UserRole.LOCAL):
                base = base.where(Job.user_id == user.id)
            active = sess.exec(
                base.where(sq.col(Job.is_done).is_(False)).order_by(sq.asc(Job.created_at))
            ).all()
            finished = sess.exec(
                base.where(sq.col(Job.is_done).is_(True))
                .order_by(sq.desc(Job.updated_at))
                .limit(recent)
            ).all()
        rows = []
        for job in list(active) + list(finished):
            extra = dict(job.extra or {})
            for key in self._BULKY_EXTRA:
                value = extra.pop(key, None)
                if isinstance(value, list):
                    extra[f"{key}_count"] = len(value)
            rows.append(
                {
                    "id": job.id,
                    "type": int(job.type),
                    "status": job.status.name,
                    "title": job.job_title,
                    "done": job.done,
                    "total": job.total,
                    "error": job.error,
                    "created_at": job.created_at,
                    "updated_at": job.updated_at,
                    "started_at": job.started_at,
                    "finished_at": job.finished_at,
                    "is_done": job.is_done,
                    "extra": extra,
                }
            )
        return rows

    def pause(self, job_id: str) -> None:
        """Hold a bulk download where it is; resume() picks it up from its checkpoint.

        A paused job is never picked by the scheduler and is left alone by the
        clean-up of stuck jobs and by restarts, so it stays paused until resumed.
        """
        job = self.get(job_id)
        if job.type != JobType.EXPORT_SOURCE:
            raise ServerErrors.forbidden.with_extra("Only site downloads can be paused")
        if job.is_done:
            return
        with ctx.db.session() as sess:
            self._update(sess, job_id, status=JobStatus.PAUSED)
            sess.commit()

    def resume(self, job_id: str) -> None:
        """Put a paused download back in the queue (it continues where it stopped)."""
        job = self.get(job_id)
        if job.status != JobStatus.PAUSED:
            return
        with ctx.db.session() as sess:
            self._update(sess, job_id, status=job_pending_literal)
            sess.commit()

    def cancel(self, job_id: str, who: str = "admin") -> None:
        with ctx.db.session() as sess:
            self._cancel_down(sess, job_id, True)
            self._fail(
                sess,
                job_id,
                reason=f"Canceled as the parent job was canceled by {who}",
            )
            self._update(
                sess,
                job_id,
                error=f"Canceled by {who}",
                status=job_canceled_literal,
            )
            sess.commit()

    # -------------------------------------------------------------------------
    #                            Internal Methods
    # -------------------------------------------------------------------------
    def _get_active_job_count(
        self,
        sess: Session,
        user_id: str,
        types: Optional[List[JobType]] = None,
    ):
        stmt = sq.select(sq.func.count())
        stmt = stmt.select_from(Job)
        stmt = stmt.where(
            Job.user_id == user_id,
            sq.col(Job.is_done).is_(False),
            sq.col(Job.parent_job_id).is_(None),
        )
        if types:
            stmt = stmt.where(sq.col(Job.type).in_(types))
        return sess.scalar(stmt) or 0

    def _ensure_user_access_limit(self, sess: Session, user: User):
        limit = ctx.tier.max_active_jobs(user)
        if limit is not None:
            active = self._get_active_job_count(sess, user.id)
            if active >= limit:
                raise ServerErrors.job_limit_reached.with_extra(active)

        search_limit = ctx.tier.max_active_search_jobs(user)
        if search_limit is not None:
            active = self._get_active_job_count(
                sess,
                user.id,
                types=[
                    JobType.SEARCH_SOURCE,
                    JobType.SEARCH_ALL_SOURCES,
                ],
            )
            if active >= search_limit:
                raise ServerErrors.search_job_limit_reached.with_extra(active)

    def _create(
        self,
        user: User,
        type: JobType,
        data: dict,
        parent_id: Optional[str] = None,
        depends_on: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> Job:
        with ctx.db.session() as sess:
            if parent_id is None:
                self._ensure_user_access_limit(sess, user)

            job = Job(
                type=type,
                extra=data,
                user_id=user.id,
                depends_on=depends_on,
                parent_job_id=parent_id,
                priority=ctx.tier.job_priority(user),
            )
            if job_id:
                job.id = job_id  # reserved by a batch that runs it (JobRunner.reserve)
            sess.add(job)

            self._update_up(
                sess,
                job.id,
                total=Job.total + 1,
            )

            sess.commit()
            sess.refresh(job)

        if parent_id is None:
            ctx.activity.record(user.id, ActivityType.REQUEST, job.id)
        return job

    def _pending(
        self,
        artifact: Optional[bool] = None,
        skip_job_ids: Iterable[str] = [],
        skip_user_ids: Iterable[str] = [],
        held_exports: Iterable[Tuple[str, str]] = (),
    ) -> Optional[Job]:
        """The next job to run. ``held_exports`` is (job id, site) of the exports
        workers in this process still hold, cancelled ones included."""
        with ctx.db.session() as sess:
            stmt = sq.select(Job)

            job_alias = aliased(Job)
            dep_is_done = (
                sq.exists(1)
                .where(sq.col(job_alias.id) == Job.depends_on)
                .where(sq.col(job_alias.is_done).is_(True))
            )
            stmt = stmt.where(sq.or_(sq.col(Job.depends_on).is_(None), dep_is_done))

            job_is_new = sq.and_(
                Job.status == JobStatus.RUNNING,
                Job.done == 0,
            )
            stmt = stmt.where(
                sq.or_(
                    Job.status == JobStatus.PENDING,
                    job_is_new,
                )
            )

            if skip_job_ids:
                stmt = stmt.where(sq.col(Job.id).not_in(skip_job_ids))

            if skip_user_ids:
                stmt = stmt.where(
                    sq.or_(
                        Job.priority != JobPriority.LOW,
                        sq.col(Job.user_id).not_in(skip_user_ids),
                    )
                )

            if artifact is not None:
                if artifact:
                    stmt = stmt.where(Job.type == JobType.ARTIFACT)
                else:
                    stmt = stmt.where(Job.type != JobType.ARTIFACT)

            stmt = stmt.order_by(
                sq.desc(Job.priority),
                sq.asc(Job.updated_at),
            )
            # Bulk downloads (source exports) run one per site, so a site only
            # ever sees one job's requests and one speed limit, while different
            # sites download side by side, up to a share of the workers that
            # leaves room for everything else. A site's other exports wait.
            other = sess.exec(stmt.where(Job.type != JobType.EXPORT_SOURCE).limit(1)).first()
            exports = sess.exec(stmt.where(Job.type == JobType.EXPORT_SOURCE).limit(200)).all()
            export = None
            if exports:
                running = list(held_exports) + [
                    (job.id, str((job.extra or {}).get("domain") or ""))
                    for job in sess.exec(
                        sq.select(Job)
                        .where(Job.type == JobType.EXPORT_SOURCE)
                        .where(Job.status == JobStatus.RUNNING)
                    ).all()
                ]
                running = list(dict(running).items())  # once per job
                for job in exports:
                    site = str((job.extra or {}).get("domain") or "")
                    others = [domain for job_id, domain in running if job_id != job.id]
                    if site not in others and len(others) < max_parallel_exports():
                        export = job
                        break
            if other is None or export is None:
                return other or export
            # whichever the ordering above puts first
            if (-other.priority, other.updated_at) <= (-export.priority, export.updated_at):
                return other
            return export

    # Job rows are changed with bulk UPDATE statements, which the ORM's
    # before_update hook never sees, so each one stamps updated_at itself. That
    # keeps updated_at meaning "last activity", which the scrubber relies on to
    # tell a stuck job from one that is simply long.
    def _update(self, sess: Session, job_id: str, **values) -> None:
        values.setdefault("updated_at", current_timestamp())
        sess.exec(
            sq.update(Job)
            .where(
                sq.col(Job.id) == job_id,
            )
            .values(**values)
        )

    def _update_up(
        self,
        sess: Session,
        job_id: str,
        done=Job.done,
        total=Job.total,
        failed=Job.failed,
        inclusive: bool = False,
    ) -> None:
        now = current_timestamp()

        sa_done = done
        sa_total = total
        sa_failed = failed
        sa_is_done = sa_done == sa_total

        sa_status = sq.case(
            (sa_is_done, sq.case((sa_failed > 0, job_failed_literal), else_=job_success_literal)),
            else_=Job.status,
        )
        sa_started_at = sq.case(
            (sq.and_(sa_is_done, sq.col(Job.started_at).is_(None)), now),
            else_=Job.started_at,
        )
        sa_finished_at = sq.case(
            (sq.and_(sa_is_done, sq.col(Job.finished_at).is_(None)), now),
            else_=Job.finished_at,
        )

        sa_pars = select_ancestors(job_id, inclusive)
        sess.exec(
            sq.update(Job)
            .where(sq.col(Job.id).in_(sa_pars))
            .where(sq.col(Job.is_done).is_(False))
            .values(
                done=sa_done,
                total=sa_total,
                failed=sa_failed,
                status=sa_status,
                is_done=sa_is_done,
                started_at=sa_started_at,
                finished_at=sa_finished_at,
                updated_at=now,
            )
        )

    def _cancel_down(self, sess: Session, job_id: str, inclusive=False) -> None:
        now = current_timestamp()
        sa_deps = select_descendants(job_id, inclusive)
        sess.exec(
            sq.update(Job)
            .where(
                sq.col(Job.id).in_(sa_deps),
                sq.col(Job.is_done).is_(False),
            )
            .values(
                is_done=True,
                status=job_canceled_literal,
                error="Canceled by one of the parent",
                started_at=sq.func.coalesce(Job.started_at, now),
                finished_at=sq.func.coalesce(Job.finished_at, now),
                updated_at=now,
            )
        )

    def _increment_up(self, sess: Session, job_id: str, step: int = 1) -> None:
        self._update_up(
            sess,
            job_id=job_id,
            inclusive=True,
            done=Job.done + step,
        )

    def _count_pending(self, sess: Session, job_id: str) -> int:
        return sess.exec(sq.select(Job.total - Job.done).where(Job.id == job_id)).one()

    def _success(self, sess: Session, job_id: str) -> None:
        pending = self._count_pending(sess, job_id)
        self._increment_up(sess, job_id, pending)

    def _fail(self, sess: Session, job_id: str, reason: str) -> None:
        pending = self._count_pending(sess, job_id)
        self._update_up(
            sess,
            job_id=job_id,
            inclusive=True,
            done=Job.done + pending,
            failed=Job.failed + pending,
        )
        self._update(
            sess,
            job_id,
            error=reason,
            status=job_failed_literal,
        )

    def cancel_if_dangling(self, job: Job) -> bool:
        root = self.get_root(job.id)
        if root and not root.is_done:
            return False

        who = "the app: the job it belonged to had already ended"
        self.cancel(job.id, who)
        if root:
            if root.status == JobStatus.CANCELED:
                self.cancel(root.id, who)
            else:
                with ctx.db.session() as sess:
                    self._cancel_down(sess, root.id, False)
                    sess.commit()

        return True

    def update_extra(
        self,
        job: Union[Job, str],
        updates: Union[dict, Callable[[dict], None]],
    ) -> None:
        with ctx.db.session() as sess:
            job_id = job.id if isinstance(job, Job) else job
            current = sess.scalar(sq.select(Job.extra).where(Job.id == job_id))

            extra = dict(current or {})
            if callable(updates):
                updates(extra)
            else:
                extra.update(updates)

            sess.exec(
                sq.update(Job)
                .where(sq.col(Job.id) == job_id)
                .values(extra=extra, updated_at=current_timestamp())
            )
            sess.commit()

            if isinstance(job, Job):
                job.extra = extra
