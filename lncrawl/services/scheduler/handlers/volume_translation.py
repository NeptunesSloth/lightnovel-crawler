from typing import List

from ....context import ctx
from ....dao import Job
from ....enums import JobType
from ._base import BatchHandler, HandlerException


class VolumeTranslationHandler(BatchHandler):
    @staticmethod
    def can_activate(job) -> bool:
        return job.type == JobType.VOLUME_TRANSLATION

    def run(self) -> None:
        volume_id = self.job.extra.get("volume_id")
        if not volume_id:
            raise HandlerException("No volume id")

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
                    language=language,
                    parent_id=self.job.id,
                    job_id=job_id,
                    novel_title=self.job.extra.get("novel_title"),
                )
                for chapter_id, job_id in zip(keys, job_ids)
            ]

        chapter_ids = ctx.chapters.list_ids(volume_id=volume_id)
        self._run_children_inline("chapter_id", chapter_ids, JobType.CHAPTER_TRANSLATION, create)

        volume = ctx.volumes.get(volume_id)
        ctx.translator.translate_volume(volume, language, self.signal)
