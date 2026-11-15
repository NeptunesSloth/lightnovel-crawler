from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import threading
from threading import Event
from time import monotonic
from typing import Iterator, Optional

from ..exceptions import AbortedException


class CrawlTimeoutError(TimeoutError):
    """One chapter exhausted its budget; other chapters can still continue."""


@dataclass
class RequestBudget:
    """How long the site may take to answer one chapter or one novel's details.

    The clock stands still while any of the budget's requests waits its turn to
    be sent: for a slot in the export's shared rate, or for the scraper's
    connection slot and gap. Those queues are this program's own, and a novel
    whose chapter list waited behind three other novels' chapters used to time
    out after ten minutes without the site having been slow at all. The clock
    stays stopped while any of its requests is waiting, even if another one is
    being answered meanwhile; each request still has its own network timeouts.
    """

    seconds: float
    signal: Optional[Event] = None
    label: str = "Chapter request"
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _waiting: int = field(default=0, init=False, repr=False)
    _paused_at: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        self.deadline = monotonic() + self.seconds

    def _now(self) -> float:
        with self._lock:
            return self._paused_at if self._waiting else monotonic()

    def is_set(self) -> bool:
        return bool(self.signal and self.signal.is_set()) or self._now() >= self.deadline

    def remaining(self) -> float:
        if self.signal and self.signal.is_set():
            raise AbortedException("Crawl cancelled")
        remaining = self.deadline - self._now()
        if remaining <= 0:
            raise CrawlTimeoutError(f"{self.label} timed out after {self.seconds:g} seconds")
        return remaining

    @contextmanager
    def paused(self) -> Iterator[None]:
        """Stop the clock while the calling thread waits for its turn to send."""
        with self._lock:
            if not self._waiting:
                self._paused_at = monotonic()
            self._waiting += 1
        try:
            yield
        finally:
            with self._lock:
                self._waiting -= 1
                if not self._waiting:
                    self.deadline += monotonic() - self._paused_at


_budget: ContextVar[Optional[RequestBudget]] = ContextVar("crawl_request_budget", default=None)


def current_budget() -> Optional[RequestBudget]:
    return _budget.get()


@contextmanager
def waiting_turn() -> Iterator[None]:
    """Mark a wait for this program's own throttles; the current budget's clock stops."""
    budget = current_budget()
    if budget is None:
        yield
        return
    with budget.paused():
        yield


@contextmanager
def request_budget(
    signal: Optional[Event], seconds: float = 60, label: str = "Chapter request"
) -> Iterator[RequestBudget]:
    existing = current_budget()
    budget = existing or RequestBudget(seconds, signal, label)
    token = _budget.set(budget)
    try:
        budget.remaining()
        yield budget
    finally:
        _budget.reset(token)
