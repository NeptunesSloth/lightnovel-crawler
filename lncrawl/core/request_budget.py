from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Event
from time import monotonic
from typing import Iterator, Optional

from ..exceptions import AbortedException


class CrawlTimeoutError(TimeoutError):
    """One chapter exhausted its budget; other chapters can still continue."""


@dataclass
class RequestBudget:
    seconds: float
    signal: Optional[Event] = None

    def __post_init__(self) -> None:
        self.deadline = monotonic() + self.seconds

    def is_set(self) -> bool:
        return bool(self.signal and self.signal.is_set()) or monotonic() >= self.deadline

    def remaining(self) -> float:
        if self.signal and self.signal.is_set():
            raise AbortedException("Crawl cancelled")
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise CrawlTimeoutError(f"Chapter request timed out after {self.seconds:g} seconds")
        return remaining


_budget: ContextVar[Optional[RequestBudget]] = ContextVar("crawl_request_budget", default=None)


def current_budget() -> Optional[RequestBudget]:
    return _budget.get()


@contextmanager
def request_budget(signal: Optional[Event], seconds: float = 60) -> Iterator[RequestBudget]:
    existing = current_budget()
    budget = existing or RequestBudget(seconds, signal)
    token = _budget.set(budget)
    try:
        budget.remaining()
        yield budget
    finally:
        _budget.reset(token)
