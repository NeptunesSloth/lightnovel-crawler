"""A doorbell for the scheduler's idle job runners.

Idle runners used to each query the database every second for something to do,
which was a tenth of the app's CPU during a download. Now they sleep until a
ring (a job was created, resumed or finished) and only poll as a slow safety
net. Kept free of imports so job creation can ring without loading the
scheduler.
"""

from threading import Condition


class Wakeup:
    """Wakes one waiter per ring; a ring with nobody waiting is kept for the next.

    Kept rings are capped, so a burst of rings wakes at most ``limit`` checks.
    """

    def __init__(self, limit: int = 8) -> None:
        self._condition = Condition()
        self._rings: int = 0
        self._limit = limit

    def ring(self) -> None:
        with self._condition:
            self._rings = min(self._limit, self._rings + 1)
            self._condition.notify()

    def ring_all(self) -> None:
        with self._condition:
            self._rings = self._limit
            self._condition.notify_all()

    def wait(self, timeout: float) -> bool:
        """Sleep until rung or ``timeout`` seconds pass. True if rung."""
        with self._condition:
            if not self._rings:
                self._condition.wait(timeout)
            if self._rings:
                self._rings -= 1
                return True
            return False


# Runners of every job but artifacts, and the artifact maker: separate, so a
# new artifact job never wakes a runner that would find nothing to do.
job_wakeup = Wakeup()
artifact_wakeup = Wakeup()


def ring_for(is_artifact: bool) -> None:
    (artifact_wakeup if is_artifact else job_wakeup).ring()
