from typing import List

from ....context import ctx
from ....enums import JobType
from ._base import AbortedException, BatchHandler, HandlerException


class DiscoverSourceHandler(BatchHandler):
    @staticmethod
    def can_activate(job) -> bool:
        return job.type == JobType.DISCOVER_SOURCE

    def run(self) -> None:
        domain = self.job.extra.get("domain")
        if not domain:
            raise HandlerException("Source domain is not specified")

        if not self.job.is_running:
            self._set_running()

        # Once the fetch batch is queued, only wait for the children to finish.
        for child in self.children:
            if child.type in (JobType.NOVEL_BATCH, JobType.FULL_NOVEL_BATCH):
                return

        urls = self._discover_novels(domain)
        if not urls:
            return
        if not ctx.tier.search_can_fetch_novel_metadata(self.user):
            return

        if self.signal.is_set():
            raise AbortedException()
        ctx.jobs.fetch_many_novels(
            self.user,
            *urls,
            full=bool(self.job.extra.get("full")),
            parent_id=self.job.id,
        )

    def _discover_novels(self, domain: str) -> List[str]:
        # Resume from a previous run instead of hitting the network again.
        existing = self.job.extra.get("discovered")
        if existing:
            return list(existing)

        if self.signal.is_set():
            raise AbortedException()
        urls = ctx.crawler.discover_novels(self.user.id, domain, self.signal)
        if self.signal.is_set():
            raise AbortedException()

        self._set_extra(discovered=urls)
        return urls
