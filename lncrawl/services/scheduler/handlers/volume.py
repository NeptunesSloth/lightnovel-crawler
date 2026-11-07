from ....context import ctx
from ....dao import Job
from ....enums import JobType
from ._base import BatchHandler, HandlerException


class VolumeHandler(BatchHandler):
    @staticmethod
    def can_activate(job) -> bool:
        return job.type == JobType.VOLUME

    child_key = "chapter_id"
    empty_done = False

    def run(self) -> None:
        volume_id = self.job.extra.get("volume_id")
        if not volume_id:
            raise HandlerException("No volume id")

        if not self.job.is_running:
            self._set_running()

        def create(chapter_id: str) -> Job:
            return ctx.jobs.fetch_chapter(
                self.user,
                chapter_id,
                parent_id=self.job.id,
                novel_title=self.job.extra.get("novel_title"),
            )

        chapter_ids = ctx.chapters.list_ids(volume_id=volume_id)
        self._run_children_inline("chapter_id", chapter_ids, create)
