"""Request pacing for source exports, adjusted request by request.

The export used to change its speed only between novels: it halved the rate when
a novel failed in a way that looked like throttling and raised it again only
after three novels came out complete. On a slow site with big novels that meant
one slow chapter list could drop the rate to its floor for hours, because every
big novel then failed the same way and three clean novels in a row never came.

Here every finished chapter or image is a data point. A refusal from the site
slows the export straight away; a run of successes speeds it back up a step at a
time, up to the ceiling. Both lanes of a parallel export report to the same
controller and send through the same limiter, so together they keep one rate.
"""

from collections import deque
import threading
from time import monotonic
from typing import Callable, Deque, List, Optional, Tuple

from ....utils.ratelimit import RateLimiter

RPS_MIN = 0.2  # slowest the controller goes (1 request / 5s)
RPS_MAX = 5.0  # fastest it goes when no rate was asked for
RPS_UNCAPPED_START = 1.0  # where an uncapped run lands on its first refusal

# A refusal halves the rate; a slow site (requests that time out or drop) only
# trims it, because waiting longer between requests helps a busy site a little
# but cannot make a slow page fast.
HARD_CUT = 0.5
SOFT_CUT = 0.85
CUT_COOLDOWN = 15.0  # seconds; requests in flight fail together, count them once

RAISE_AFTER = 20  # successes in a row before the next step up
RAISE_FACTOR = 1.25
RAISE_MIN_STEP = 0.1

PACE_WINDOW = 600.0  # seconds of history used for the time-left estimate

# A refusal: the site is telling us to slow down or has started blocking us.
_REFUSED = (
    "429",
    "403",
    "too many",
    "rate limit",
    "ratelimit",
    "blocked",
    "captcha",
    "cloudflare",
    "forbidden",
)
# Slowness: the site is busy or the connection is poor.
_SLOW = ("timeout", "timed out", "connection", "reset by peer", "502", "503", "504")


def pressure(reason: str) -> str:
    """'refused', 'slow' or '' for a failure reason."""
    low = (reason or "").lower()
    if any(hint in low for hint in _REFUSED):
        return "refused"
    if any(hint in low for hint in _SLOW):
        return "slow"
    return ""


class PaceController:
    """Holds an export's request rate and moves it as results come in.

    ``rate`` 0 means uncapped. ``ceiling`` is the fastest it may go: the rate
    the user asked for, if they asked, so tuning never goes faster than that.
    ``on_change(rate)`` is told whenever the rate moves.
    """

    def __init__(
        self,
        rate: float,
        auto: bool = True,
        ceiling: float = RPS_MAX,
        on_change: Optional[Callable[[float], None]] = None,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.rate = max(0.0, rate)
        self.auto = auto
        self.ceiling = max(ceiling, RPS_MIN)
        self.on_change = on_change
        self._clock = clock
        self._lock = threading.Lock()
        self._streak = 0
        self._last_cut = -CUT_COOLDOWN
        self._done: Deque[float] = deque()
        self.limiter: Optional[RateLimiter] = RateLimiter(self.rate) if self.rate > 0 else None
        self._taskmans: List[object] = []

    def attach(self, taskman) -> None:
        """Make ``taskman`` send through this controller's limiter."""
        with self._lock:
            self._taskmans.append(taskman)
            taskman.use_limiter(self.limiter)

    def _set(self, rate: float) -> None:
        # called with the lock held
        rate = round(rate, 2)
        if rate == self.rate:
            return
        self.rate = rate
        if self.limiter is None:
            self.limiter = RateLimiter(rate)
            for taskman in self._taskmans:
                taskman.use_limiter(self.limiter)  # type: ignore[attr-defined]
        else:
            self.limiter.set_rate(rate)
        if self.on_change:
            self.on_change(rate)

    def report(
        self, error: Optional[BaseException] = None, reason: str = "", count: bool = True
    ) -> None:
        """One request finished; ``error`` is set when it failed.

        ``count`` False keeps it out of the time-left pace (images, which are not
        part of the chapter count the estimate works in).
        """
        now = self._clock()
        with self._lock:
            if error is None and not reason and count:
                self._done.append(now)
                while self._done and now - self._done[0] > PACE_WINDOW:
                    self._done.popleft()
            if not self.auto:
                return
            if error is None and not reason:
                self._streak += 1
                if self.rate > 0 and self._streak >= RAISE_AFTER and self.rate < self.ceiling:
                    self._streak = 0
                    step = max(RAISE_MIN_STEP, self.rate * (RAISE_FACTOR - 1))
                    self._set(min(self.ceiling, self.rate + step))
                return
            kind = pressure(reason or f"{type(error).__name__}: {error}")
            if not kind:
                return  # a broken page, not the site pushing back
            self._streak = 0
            if now - self._last_cut < CUT_COOLDOWN:
                return
            self._last_cut = now
            if self.rate <= 0:
                self._set(RPS_UNCAPPED_START)
            else:
                cut = HARD_CUT if kind == "refused" else SOFT_CUT
                self._set(max(RPS_MIN, self.rate * cut))

    def pace(self) -> float:
        """Items finished per second over the recent window (0 if too few)."""
        now = self._clock()
        with self._lock:
            while self._done and now - self._done[0] > PACE_WINDOW:
                self._done.popleft()
            if len(self._done) < 5:
                return 0.0
            span = max(now - self._done[0], 1.0)
            return len(self._done) / span


def estimate_left(
    pace: float,
    active: List[Tuple[int, int]],
    novels_waiting: int,
    chapters_per_novel: float,
) -> Optional[int]:
    """Seconds left at ``pace``, or None when there is nothing to go on yet.

    ``active`` is (done, total) for the novels being downloaded now; novels not
    started yet are guessed at the average still needed by those seen so far.
    """
    if pace <= 0:
        return None
    left = sum(max(0, total - done) for done, total in active)
    left += novels_waiting * max(0.0, chapters_per_novel)
    return int(left / pace)
