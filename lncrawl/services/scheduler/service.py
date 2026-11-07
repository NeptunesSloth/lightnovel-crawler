import logging
from threading import Event, Thread
from typing import Callable, List, Optional, Set

from ...context import ctx
from ...exceptions import AbortedException
from ...utils.event_lock import EventLock
from ...utils.wakeup import Wakeup, artifact_wakeup, job_wakeup
from .runner import JobRunner
from .scrubber import Scrubber

logger = logging.getLogger(__name__)

# How often an idle job runner looks for work it was not woken for.
IDLE_POLL_SECONDS: float = 5.0


def run_scrubber(signal: Event) -> None:
    Scrubber.run(signal)


def run_jobs(signal: Event) -> bool:
    return JobRunner.run(signal, False)


def run_artifact_maker(signal: Event) -> bool:
    return JobRunner.run(signal, True)


class JobScheduler:
    def __init__(self) -> None:
        self._threads: List[Thread] = []
        self._queue: Set[str] = set()
        self._lock = EventLock()
        self._signal = Event()
        self._signal.set()

    def close(self):
        self.stop()

    @property
    def running(self) -> bool:
        return not self._signal.is_set()

    def start(self):
        if self.running:
            return
        self._signal = Event()
        # Recover jobs left RUNNING by a previous process (app closed mid-run) so
        # they resume instead of sitting orphaned. Safe here: no worker is running
        # yet, so every RUNNING job is stale.
        try:
            ctx.jobs.requeue_stale_running()
        except Exception:
            logger.error("Failed to requeue stale running jobs", exc_info=True)
        # Force nodriver + its CDP submodules to import now, in this single thread,
        # before any worker can race the first import. nodriver.cdp is a web of
        # circular submodule imports that is only safe to use once fully loaded; a
        # concurrent first import (a batch export hitting several JS-rendered novels)
        # can otherwise leave it half-initialized and crash every later browser op
        # with "cannot import name 'network' from partially initialized module".
        try:
            from ...webdriver import (
                BROWSER_PAGES_SUPPORTED,
                BROWSER_PAGES_UNSUPPORTED,
                warmup_nodriver,
            )

            if BROWSER_PAGES_SUPPORTED:
                warmup_nodriver()
            else:
                logger.warning(BROWSER_PAGES_UNSUPPORTED)
        except Exception:
            logger.warning("nodriver warmup failed; will retry on first browser use", exc_info=True)
        for _ in range(ctx.config.crawler.runner_concurrency):
            self._thread(run_jobs, ctx.config.crawler.runner_cooldown, job_wakeup)
        self._thread(run_scrubber, ctx.config.crawler.scrubber_cooldown)
        self._thread(run_artifact_maker, ctx.config.crawler.runner_cooldown, artifact_wakeup)
        logger.info("Scheduler started")

    def stop(self):
        if not self.running:
            return
        self._signal.set()
        job_wakeup.ring_all()
        artifact_wakeup.ring_all()
        JobRunner.cancel_all()
        for t in self._threads:
            t.join()
        self._threads.clear()
        logger.info("Scheduler stoppped")

    def stop_job(self, job_id: str):
        JobRunner.cancel(job_id)

    def _thread(
        self,
        run: Callable[[Event], Optional[bool]],
        interval: float,
        wakeup: Optional[Wakeup] = None,
    ) -> None:
        t = Thread(
            target=self._loop,
            args=[run, interval, wakeup],
            daemon=True,  # does not block exit
        )
        t.start()
        self._threads.append(t)

    def _loop(
        self,
        run: Callable[[Event], Optional[bool]],
        interval: float,
        wakeup: Optional[Wakeup] = None,
    ) -> None:
        while self.running:
            try:
                # A runner that just took a job goes straight back for the next
                # one; the cooldown is only for an empty queue. Pausing after every
                # job capped a novel at about one chapter per second per worker,
                # since each chapter (and each image) is a job of its own.
                # Only a runner reporting True took a job; anything else (the
                # scrubber returns None) waits, so a loop can never spin.
                busy = run(self._signal) is True
                if not busy:
                    if wakeup is None:
                        self._signal.wait(interval)
                    else:
                        # sleep until a job is created, resumed or finishes;
                        # poll slowly for anything that becomes ready unrung
                        wakeup.wait(max(interval, IDLE_POLL_SECONDS))
                if self._signal.is_set():
                    return
            except KeyboardInterrupt:
                self._signal.set()
            except AbortedException:
                return
            except Exception:
                logger.error("Unexpected error in scheduler", exc_info=True)
                self._signal.wait(interval)  # don't spin on an error that repeats
