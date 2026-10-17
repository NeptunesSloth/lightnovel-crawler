import logging
import threading
import time
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Longest single sleep before re-checking for shutdown, so closing a limiter that
# has threads parked on a slow rate does not wait out the whole interval.
_SLEEP_SLICE = 0.25


class RateLimiter(object):
    """Admits calls at a fixed rate — exactly, however many threads are waiting.

    Each caller takes the next free slot on a shared clock while holding the lock,
    and only then sleeps until that slot comes round. Reserving before sleeping is
    the whole point: threads that read a "last sent at" timestamp and then sleep
    on it all wake together and arrive as a burst, so a pool of N workers sends
    up to N requests per interval while believing it is sending one. Measured
    against a local server, that overshoot was about 2x.

    Used by TaskManager, which no longer has to pin itself to a single worker to
    keep a rate honest — so a download can overlap another's latency without ever
    asking the site faster than it was told to.

    Args:
    - ratelimit (float): Number of requests per second.
    """

    def __init__(self, ratelimit: float):
        if ratelimit <= 0:
            raise ValueError("ratelimit should be a non-zero positive number")
        self.period = 1 / ratelimit
        self._lock = threading.Lock()
        self._next_at = 0.0
        self._closed = False

    def _now(self) -> float:
        return time.monotonic()

    def acquire(self) -> None:
        """Block until it is this caller's turn to send."""
        if self._closed:
            return
        now = self._now()
        with self._lock:
            start = self._next_at if self._next_at > now else now
            self._next_at = start + self.period
        remaining = start - now
        while remaining > 0 and not self._closed:
            step = min(_SLEEP_SLICE, remaining)
            time.sleep(step)
            remaining -= step

    def shutdown(self) -> None:
        self._closed = True

    def wrap(self, fn: Callable[..., T]) -> Callable[..., T]:
        def inner(*args: Any, **kwargs: Any) -> T:
            self.acquire()
            return fn(*args, **kwargs)

        return inner

    def __enter__(self) -> "RateLimiter":
        self.acquire()
        return self

    def __exit__(self, type: Any, value: Any, traceback: Any) -> None:
        return None
