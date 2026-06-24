import re
from typing import Dict, List

from ....context import ctx
from ....enums import JobType
from ._base import BaseHandler, HandlerException

# Pulls numbers out of a source's free-text search info (e.g. "1.2K chapters | 3.4M views")
# so we can rank by the site's own figures. Handles K/M/B suffixes.
_NUM_RE = re.compile(r"(\d[\d,]*\.?\d*)\s*([KMB])?", re.IGNORECASE)
_SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9}


def _score(info: str) -> float:
    """Best-effort rank score: the largest number in the info text (views/rank)."""
    best = 0.0
    for num, suffix in _NUM_RE.findall(info or ""):
        try:
            value = float(num.replace(",", ""))
        except ValueError:
            continue
        if suffix:
            value *= _SUFFIX.get(suffix.lower(), 1)
        best = max(best, value)
    return best


class ListSourceHandler(BaseHandler):
    """Discover the novels a source exposes and store a ranked list (no downloads).

    Unlike the export/discover jobs, this only enumerates novels and ranks them
    by each site's own numbers (parsed from the search info), so the UI can show
    a pick-list. The ranked results land in ``job.extra["results"]``.
    """

    @staticmethod
    def can_activate(job) -> bool:
        return job.type == JobType.LIST_SOURCE

    def run(self) -> None:
        domain = self.job.extra.get("domain")
        if not domain:
            raise HandlerException("Source domain is not specified")

        self._set_running()

        items = ctx.crawler.discover_search_results(self.user.id, domain, self.signal)
        results: List[Dict] = [
            {
                "title": item.title,
                "url": item.url,
                "info": item.info,
                "score": _score(item.info),
            }
            for item in items
        ]
        # rank by the site's own figure, then by title for stable ordering
        results.sort(key=lambda r: (-r["score"], r["title"].lower()))

        self._set_extra(results=results, total_novels=len(results))
