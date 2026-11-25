from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import threading
from threading import Event
from time import monotonic
from typing import Dict, Iterator, Optional

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

    ``wall`` is a limit the waiting does not stop: a novel's chapter list that
    kept going behind other novels' chapters was "loading" for hours, its clock
    hardly running, with one of the export's few download slots held all along.
    """

    seconds: float
    signal: Optional[Event] = None
    label: str = "Chapter request"
    wall: Optional[float] = None
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _waiting: int = field(default=0, init=False, repr=False)
    _paused_at: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        self.deadline = monotonic() + self.seconds
        self.wall_deadline: Optional[float] = monotonic() + self.wall if self.wall else None

    def _past_wall(self) -> bool:
        return self.wall_deadline is not None and monotonic() >= self.wall_deadline

    def _now(self) -> float:
        with self._lock:
            return self._paused_at if self._waiting else monotonic()

    def is_set(self) -> bool:
        if self.signal and self.signal.is_set():
            return True
        return self._now() >= self.deadline or self._past_wall()

    def remaining(self) -> float:
        if self.signal and self.signal.is_set():
            raise AbortedException("Crawl cancelled")
        remaining = self.deadline - self._now()
        if remaining <= 0:
            raise CrawlTimeoutError(f"{self.label} timed out after {self.seconds:g} seconds")
        if self.wall_deadline is not None:
            left = self.wall_deadline - monotonic()
            if left <= 0:
                raise CrawlTimeoutError(
                    f"{self.label} was still going after {(self.wall or 0) / 60:g} minutes"
                )
            remaining = min(remaining, left)
        return remaining

    @contextmanager
    def paused(self) -> Iterator[None]:
        """Stop the clock while the calling thread waits for its turn to send."""
        with self._lock:
            if not self._waiting:
                self._paused_at = monotonic()
                _count_waiting(self.signal, 1)
            self._waiting += 1
        try:
            yield
        finally:
            with self._lock:
                self._waiting -= 1
                if not self._waiting:
                    self.deadline += monotonic() - self._paused_at
                    _count_waiting(self.signal, -1)


# How many budgets with each cancellation signal have a request waiting its
# turn right now: what a batch's stall watchdog asks (see waiting_for).
_waiting_lock = threading.Lock()
_waiting_by_signal: Dict[Event, int] = {}


def _count_waiting(signal: Optional[Event], change: int) -> None:
    if signal is None:
        return
    with _waiting_lock:
        count = _waiting_by_signal.get(signal, 0) + change
        if count > 0:
            _waiting_by_signal[signal] = count
        else:
            _waiting_by_signal.pop(signal, None)


def waiting_for(signal: Event) -> int:
    """How many chapters or images cancelled by ``signal`` wait for their turn to be sent."""
    with _waiting_lock:
        return _waiting_by_signal.get(signal, 0)


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
    signal: Optional[Event],
    seconds: float = 60,
    label: str = "Chapter request",
    wall: Optional[float] = None,
) -> Iterator[RequestBudget]:
    existing = current_budget()
    budget = existing or RequestBudget(seconds, signal, label, wall)
    token = _budget.set(budget)
    try:
        budget.remaining()
        yield budget
    finally:
        _budget.reset(token)
