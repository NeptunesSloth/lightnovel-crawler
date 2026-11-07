from typing import List

from ....context import ctx
from ....dao import Job
from ....enums import JobType
from ._base import BatchHandler


class ImageBatchHandler(BatchHandler):
    @staticmethod
    def can_activate(job) -> bool:
        return job.type == JobType.IMAGE_BATCH

    def run(self) -> None:
        image_ids = self.job.extra.get("image_ids")
        if not image_ids:
            return

        if not self.job.is_running:
            self._set_running()

        def create(keys: List[str], job_ids: List[str]) -> List[Job]:
            return [
                ctx.jobs.fetch_image(
                    self.user,
                    image_id,
                    parent_id=self.job.id,
                    job_id=job_id,
                    novel_id=self.job.extra.get("novel_id"),
                    novel_title=self.job.extra.get("novel_title"),
                    chapter_id=self.job.extra.get("chapter_id"),
                    chapter_title=self.job.extra.get("chapter_title"),
                )
                for image_id, job_id in zip(keys, job_ids)
            ]

        self._run_children_inline("image_id", list(image_ids), JobType.IMAGE, create)
