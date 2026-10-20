from ....context import ctx
from ....enums import JobType, OutputFormat
from ._base import AbortedException, BatchHandler, HandlerException


class FetchMissingHandler(BatchHandler):
    @staticmethod
    def can_activate(job) -> bool:
        return job.type == JobType.FETCH_MISSING

    def run(self) -> None:
        novel_id = self.job.extra.get("novel_id")
        if not novel_id:
            raise HandlerException("No novel id")

        added_types = {}
        if self.job.is_running:
            added_types = {job.type: job.id for job in self.children}
        else:
            self._set_running()

        if self.signal.is_set():
            raise AbortedException()

        novel = ctx.novels.get(novel_id)
        if JobType.CHAPTER_BATCH not in added_types:
            report = ctx.chapters.completeness(novel_id)
            self._set_extra(completeness=report.model_dump())
            if not report.total:
                raise HandlerException(
                    "No known chapters to repair. Refresh this novel from its source first."
                )
            missing_ids = [chapter.id for chapter in report.gaps]
            if missing_ids:
                job = ctx.jobs.fetch_many_chapters(
                    self.user,
                    *missing_ids,
                    parent_id=self.job.id,
                    novel_id=novel_id,
                    novel_title=novel.title,
                )
                added_types[job.type] = job.id

        if self.job.extra.get("rebuild_epub") and JobType.ARTIFACT not in added_types:
            ctx.jobs.make_artifact(
                self.user,
                novel_id,
                OutputFormat.epub,
                parent_id=self.job.id,
                novel_title=novel.title,
                depends_on=added_types.get(JobType.CHAPTER_BATCH),
                require_complete=True,
            )
