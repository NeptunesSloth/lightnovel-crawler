from ....context import ctx
from ....core import SearchResult
from ....enums import JobType
from ._base import AbortedException, BatchHandler, HandlerException


class SearchSourceHandler(BatchHandler):
    @staticmethod
    def can_activate(job) -> bool:
        return job.type == JobType.SEARCH_SOURCE

    def run(self) -> None:
        domain = self.job.extra.get("domain")
        if not domain:
            raise HandlerException("Source domain is not specified")

        query = self.job.extra.get("query") or ""
        if not query or len(query) < 2:
            raise HandlerException("Search query must be at least 2 characters long")

        if not self.job.is_running:
            self._set_running()

        # get cached results if available, and skip searching
        results = [SearchResult(**item) for item in self.job.extra.get("search_results") or []]
        if not results:
            # get search results
            results = ctx.crawler.search_novel(
                user_id=self.user.id,
                query=query,
                domain=domain,
                signal=self.signal,
            )
            search_results = [result.to_dict() for result in results]
            if not search_results:
                return

            self._set_extra(search_results=search_results)
            if self.signal.is_set():
                raise AbortedException()

            # aggregate search results in the root job (atomic read-modify-write)
            root = ctx.jobs.get_root(self.job.id)
            if root and root.id != self.job.id:
                ctx.jobs._append_search_results(root.id, search_results, query)

        if not results:
            return
        if not ctx.tier.search_can_fetch_novel_metadata(self.user):
            return

        for job in self.children:
            if job.type == JobType.NOVEL_BATCH:
                return
        else:
            if self.signal.is_set():
                raise AbortedException()
            ctx.jobs.fetch_many_novels(
                self.user,
                *(item.url for item in results),
                full=False,
                parent_id=self.job.id,
            )
