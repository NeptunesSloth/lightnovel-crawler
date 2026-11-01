from time import monotonic
from typing import List

from ....context import ctx
from ....enums import JobType
from ._base import AbortedException, BatchHandler, HandlerException

# Discovery enumerates a source's whole catalogue (its sitemap included), which on
# a big site runs to tens of thousands of novels — and this job queues a metadata
# fetch for every one of them. Cap it so one discover job can't flood the
# scheduler; ``extra["limit"]`` overrides it for a deliberate bigger run.
MAX_DISCOVERED = 2000

# How long discovery may run, as for an export. A big site's search seeds and
# sitemap can otherwise keep it going for an hour with nothing to show for it;
# ``extra["discovery_minutes"]`` overrides it.
DISCOVERY_MINUTES = 10


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
        # Queuing what was found is a VIP feature. Say so, rather than finish
        # as a success that looks like it should have downloaded something.
        if not ctx.tier.search_can_fetch_novel_metadata(self.user):
            raise HandlerException(
                f"Found {len(urls)} novel(s) on {domain}, but your account tier cannot "
                "queue downloads from a discovery. Ask an admin to upgrade your account, "
                "or download the novels one by one."
            )

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

        # report as it goes, or the page shows "RUNNING (0/1)" until the end
        def on_progress(done: int, seeds: int, found: int) -> None:
            self._set_extra(phase="discovering", searched=done, seeds=seeds, found=found)

        budget = float(self.job.extra.get("discovery_minutes") or DISCOVERY_MINUTES) * 60
        started = monotonic()
        urls = ctx.crawler.discover_novels(
            self.user.id,
            domain,
            self.signal,
            on_progress=on_progress,
            time_budget=budget,
        )
        if self.signal.is_set():
            raise AbortedException()
        capped = monotonic() - started >= budget

        cap = int(self.job.extra.get("limit") or MAX_DISCOVERED)
        total = len(urls)
        if total > cap:
            urls = urls[:cap]

        self._set_extra(
            phase="fetching",
            discovered=urls,
            total_found=total,
            truncated=total > cap,
            discovery_capped=capped,
        )
        return urls
