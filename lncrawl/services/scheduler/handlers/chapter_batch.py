from typing import List

from ....context import ctx
from ....dao import Job
from ....enums import JobType
from ._base import BatchHandler


class ChapterBatchHandler(BatchHandler):
    @staticmethod
    def can_activate(job) -> bool:
        return job.type == JobType.CHAPTER_BATCH

    def run(self) -> None:
        chapter_ids = self.job.extra.get("chapter_ids")
        if not chapter_ids:
            return

        if not self.job.is_running:
            self._set_running()

        def create(chapter_ids: List[str], job_ids: List[str]) -> List[Job]:
            return ctx.jobs.fetch_chapters_under(
                self.user,
                self.job.id,
                chapter_ids,
                job_ids,
                novel_title=self.job.extra.get("novel_title"),
            )

        self._run_children_inline("chapter_id", list(chapter_ids), JobType.CHAPTER, create)
