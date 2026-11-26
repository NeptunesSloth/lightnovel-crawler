"""Bounded chapter scheduling shared by CLI and bulk exports."""

from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass
from threading import Event
from time import monotonic
from typing import Callable, Dict, Generic, Iterable, Iterator, Optional, TypeVar

from ..exceptions import AbortedException

T = TypeVar("T")


class CrawlStalledError(TimeoutError):
    """A worker exceeded even the per-request timeout safeguards."""


@dataclass
class CrawlOutcome(Generic[T]):
    item_id: str
    value: Optional[T] = None
    error: Optional[Exception] = None


def crawl_items(
    item_ids: Iterable[str],
    submit: Callable[[str], Future[T]],
    signal: Event,
    *,
    max_inflight: int = 4,
    idle_timeout: float = 120,
    max_consecutive_failures: int = 10,
    on_progress: Optional[Callable[[int], None]] = None,
    waiting: Optional[Callable[[], int]] = None,
    wall: Optional[float] = None,
) -> Iterator[CrawlOutcome[T]]:
    """Keep a small rolling queue; one chapter failure never cancels its peers.

    ``waiting`` says how many requests are queued for this program's own
    throttles (the site's slots and gap). Nothing finishing for ``idle_timeout``
    while some are queued is the site's pace, not a hang: the timer starts over.
    ``wall`` is the longest wait for a completion that the queueing may cover.
    """
    if max_inflight < 1 or idle_timeout <= 0:
        raise ValueError("Worker count and idle timeout must be positive")
    items = iter(item_ids)
    pending: Dict[Future[T], str] = {}
    completed = 0
    failures = 0
    accepting = True
    deadline = monotonic() + idle_timeout
    wall_deadline = monotonic() + wall if wall else None

    def fill() -> None:
        while len(pending) < max_inflight:
            if signal.is_set():
                raise AbortedException("Crawl cancelled")
            item = next(items, None)
            if item is None:
                break
            pending[submit(item)] = item

    try:
        fill()
        while pending:
            if signal.is_set():
                raise AbortedException("Crawl cancelled")
            finished, _ = wait(
                pending,
                timeout=min(0.25, max(0.001, deadline - monotonic())),
                return_when=FIRST_COMPLETED,
            )
            if not finished:
                now = monotonic()
                if wall_deadline is not None and now >= wall_deadline:
                    raise CrawlStalledError(
                        f"No crawl worker completed for {(wall or 0) / 60:g} minutes"
                    )
                if now >= deadline:
                    if waiting is not None and waiting() > 0:
                        deadline = now + idle_timeout  # queued behind the site's pace
                        continue
                    raise CrawlStalledError(
                        f"No crawl worker completed for {idle_timeout:g} seconds"
                    )
                continue
            deadline = monotonic() + idle_timeout
            if wall:
                wall_deadline = monotonic() + wall
            for future in finished:
                item = pending.pop(future)
                try:
                    outcome = CrawlOutcome(item, value=future.result())
                    failures = 0
                except Exception as error:
                    if signal.is_set():
                        raise AbortedException("Crawl cancelled") from error
                    outcome = CrawlOutcome(item, error=error)
                    failures += 1
                    if failures >= max_consecutive_failures:
                        accepting = False
                completed += 1
                if on_progress is not None:
                    on_progress(completed)
                yield outcome
            if accepting:
                fill()
    finally:
        for future in pending:
            future.cancel()
