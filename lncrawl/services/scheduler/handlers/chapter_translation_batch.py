from typing import List

from ....context import ctx
from ....dao import Job
from ....enums import JobType
from ._base import BatchHandler, HandlerException


class ChapterTranslationBatchHandler(BatchHandler):
    @staticmethod
    def can_activate(job) -> bool:
        return job.type == JobType.CHAPTER_TRANSLATION_BATCH

    def run(self) -> None:
        chapter_ids = self.job.extra.get("chapter_ids")
        if not chapter_ids:
            return

        language = self.job.extra.get("language")
        if not language:
            raise HandlerException("No target language")

        if not self.job.is_running:
            self._set_running()

        def create(keys: List[str], job_ids: List[str]) -> List[Job]:
            return [
                ctx.jobs.translate_chapter(
                    self.user,
                    chapter_id,
                    language,
                    parent_id=self.job.id,
                    job_id=job_id,
                    novel_title=self.job.extra.get("novel_title"),
                )
                for chapter_id, job_id in zip(keys, job_ids)
            ]

        self._run_children_inline(
            "chapter_id", list(chapter_ids), JobType.CHAPTER_TRANSLATION, create
        )
