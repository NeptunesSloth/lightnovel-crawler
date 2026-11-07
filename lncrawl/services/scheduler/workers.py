"""Download worker processes: chapter downloads on more than one CPU core.

Python runs one thread at a time in a process, and a server download spends its
time in work that holds that one thread: parsing pages, cleaning chapter HTML,
building ORM rows. So the web server, the scheduler and every download share a
single core however many threads they have. A worker process has a core of its
own: a batch hands its chapter jobs to one, and downloads from different sites
run side by side on different cores, off the process that serves the UI.

Rules that keep this from changing what a site sees:

- One site, one process. Requests to a site are paced per process (see
  ``core/scraping.py``), so two processes on one site would each pace alone and
  together ask twice as often. A site goes to the worker already running it;
  while it runs in the main process (a batch that could not get a worker, or a
  bulk export, which always runs there), its batches stay there too.
- A worker reads the scraper's on-disk memory of sites (pacing, solved
  challenges) when it starts and never writes it: each store writes the whole
  file, so a second writer would erase what the main process learned. Instead
  it sends what it learned about its sites back, after each run and every
  minute during a long one, and the main process keeps it.
- A worker's log records are sent to the main process and logged there, so
  they reach its console and the desktop app's log file.
- Bulk site exports run in a worker too (see `JobRunner.run`), each as one long
  job; a batch for a site whose export a worker is running goes to that worker.
- The main process still owns the jobs: it creates and reserves the children
  (see `BatchHandler._run_children_inline`), hands the worker only their ids,
  and keeps them reserved until the worker reports back. If a worker cannot be
  started, or dies, the batch runs whatever is left in-process, as before.
"""

from collections import Counter
import itertools
import logging
import multiprocessing
import os
import queue
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any, Callable, Dict, List, Optional, Tuple

from ...context import ctx

logger = logging.getLogger(__name__)

# A worker with nothing to do for this long is stopped, freeing its memory.
IDLE_SECONDS: float = 300.0
# How long a cancelled run may take to wind down before a warning is logged.
CANCEL_GRACE_SECONDS: float = 30.0
# How long a stopping worker may take before it is terminated.
STOP_SECONDS: float = 10.0
# How often a worker sends what it learned about its sites during a long run.
PROFILE_SYNC_SECONDS: float = 60.0


def worker_limit() -> int:
    """How many worker processes may run, from ``crawler.worker_processes``."""
    configured = ctx.config.crawler.worker_processes
    if configured >= 0:
        return configured
    # automatic: leave a core for the web server, and stop at three
    return max(0, min(3, (os.cpu_count() or 1) - 1))


class _Worker:
    def __init__(self, boot: Dict[str, Any]) -> None:
        mp = multiprocessing.get_context("spawn")
        self.requests: Any = mp.Queue()
        self.responses: Any = mp.Queue()
        self.logs: Any = mp.Queue()
        self.process = mp.Process(
            target=_worker_main,
            args=(self.requests, self.responses, self.logs, boot),
            name="lncrawl-download-worker",
            daemon=True,
        )
        self.process.start()
        self.site: Optional[str] = None
        self.active: int = 0
        self.idle_since: float = monotonic()
        self.alive: bool = True
        self.pending: Dict[int, Tuple[Event, List[Optional[str]]]] = {}
        self._pending_lock = Lock()
        Thread(target=self._read, name="lncrawl-worker-reader", daemon=True).start()
        Thread(target=self._read_logs, name="lncrawl-worker-logs", daemon=True).start()

    def submit(self, request_id: int, job_ids: List[str], concurrency: int) -> Tuple[Event, list]:
        done = Event()
        result: List[Optional[str]] = []
        with self._pending_lock:
            self.pending[request_id] = (done, result)
        if not self.alive:
            result.append("worker process is not running")
            done.set()
            return done, result
        self.requests.put(("run", request_id, job_ids, concurrency))
        return done, result

    def cancel(self, request_id: int) -> None:
        if self.alive:
            self.requests.put(("cancel", request_id))

    def _read(self) -> None:
        while True:
            try:
                message = self.responses.get(timeout=1.0)
            except queue.Empty:
                if not self.process.is_alive():
                    break
                continue
            except (EOFError, OSError):
                break
            if message[0] == "profiles":
                from ...core.scraping import merge_profiles

                try:
                    merge_profiles(message[1])
                except Exception:
                    logger.debug("Could not keep a worker's site profiles", exc_info=True)
            elif message[0] == "done":
                _, request_id, error = message
                with self._pending_lock:
                    entry = self.pending.pop(request_id, None)
                if entry is not None:
                    entry[1].append(error)
                    entry[0].set()
        self.alive = False
        with self._pending_lock:
            entries = list(self.pending.values())
            self.pending.clear()
        for done, result in entries:
            result.append("worker process exited")
            done.set()

    def _read_logs(self) -> None:
        while self.alive or not self.logs.empty():
            try:
                record = self.logs.get(timeout=1.0)
            except queue.Empty:
                continue
            except (EOFError, OSError):
                break
            try:
                logging.getLogger(record.name).handle(record)
            except Exception:
                pass

    def stop(self) -> None:
        self.alive = False
        try:
            self.requests.put(("stop",))
        except Exception:
            pass
        self.process.join(STOP_SECONDS)
        if self.process.is_alive():
            self.process.terminate()
            self.process.join(STOP_SECONDS)


class DownloadWorkers:
    """The pool of download worker processes, one site per worker at a time."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._workers: List[_Worker] = []
        self._local: Counter = Counter()
        self._ids = itertools.count(1)
        self._closed = False

    def close(self) -> None:
        with self._lock:
            self._closed = True
            workers = list(self._workers)
            self._workers.clear()
        for worker in workers:
            worker.stop()

    def run(
        self,
        site: str,
        job_ids: List[str],
        concurrency: int,
        signal: Event,
        fallback: Callable[[], object],
        export: bool = False,
    ) -> None:
        """Run these jobs on the worker for ``site``, or call ``fallback`` to run them here.

        ``fallback`` also runs when the worker fails part way, to finish what it
        left. Either way ``site`` counts as downloading in this process while it
        runs, so no worker takes the site meanwhile. ``export``: the job is the
        site's bulk export, already counted as holding the site.
        """
        worker = self._acquire(site, export)  # None: already counted as local
        if worker is not None:
            try:
                finished = self._run_on(worker, site, job_ids, concurrency, signal)
            finally:
                self._release(worker)
            if finished:
                return
            with self._lock:
                self._local[site] += 1
        try:
            fallback()
        finally:
            with self._lock:
                self._local[site] -= 1
                if self._local[site] <= 0:
                    del self._local[site]

    def _run_on(
        self,
        worker: "_Worker",
        site: str,
        job_ids: List[str],
        concurrency: int,
        signal: Event,
    ) -> bool:
        request_id = next(self._ids)
        done, result = worker.submit(request_id, job_ids, concurrency)
        cancelled_at: Optional[float] = None
        warned = False
        # Like a job run here, this returns once the jobs have stopped: until
        # then the worker is still on this site and must not be given another.
        while not done.wait(0.5):
            if signal.is_set():
                if cancelled_at is None:
                    worker.cancel(request_id)
                    cancelled_at = monotonic()
                elif not warned and monotonic() - cancelled_at > CANCEL_GRACE_SECONDS:
                    logger.warning(f"Download worker is slow to stop for {site}")
                    warned = True
        error = result[0] if result else "no result"
        if error is None or signal.is_set():
            return True
        logger.warning(f"Download worker failed for {site}: {error}")
        return False

    def _acquire(self, site: str, export: bool = False) -> Optional[_Worker]:
        """The worker for ``site``, or None after counting ``site`` as local."""
        from .runner import held_export_sites

        stale: List[_Worker] = []
        try:
            with self._lock:
                now = monotonic()
                for worker in list(self._workers):
                    if not worker.alive or (
                        worker.active == 0 and now - worker.idle_since > IDLE_SECONDS
                    ):
                        self._workers.remove(worker)
                        stale.append(worker)
                # the worker already on this site (a batch, or its export) takes it
                for worker in self._workers:
                    if worker.alive and worker.site == site and worker.active:
                        worker.active += 1
                        return worker
                # never a second process on a site this one is already downloading
                held_here = not export and site in held_export_sites()
                if self._closed or self._local.get(site) or held_here:
                    self._local[site] += 1
                    return None
                for worker in self._workers:
                    if worker.alive and not worker.active:
                        worker.site = site
                        worker.active = 1
                        return worker
                if len(self._workers) >= worker_limit():
                    self._local[site] += 1
                    return None
                try:
                    worker = _Worker(_boot_args())
                except Exception:
                    logger.warning("Could not start a download worker", exc_info=True)
                    self._local[site] += 1
                    return None
                worker.site = site
                worker.active = 1
                self._workers.append(worker)
                return worker
        finally:
            for worker in stale:
                Thread(target=worker.stop, daemon=True).start()

    def _release(self, worker: _Worker) -> None:
        with self._lock:
            worker.active -= 1
            if worker.active <= 0:
                worker.active = 0
                worker.idle_since = monotonic()


def _boot_args() -> Dict[str, Any]:
    config_file = ctx.config.config_file
    level = ctx.logger.level
    return {
        "config_file": str(config_file) if config_file else None,
        "log_level": logging.getLevelName(level) if level else None,
        # what the main process records (the desktop app's log file can ask
        # for more than its console shows)
        "root_level": logging.getLogger().getEffectiveLevel(),
    }


# --------------------------------------------------------------------------- #
#                         Inside the worker process                           #
# --------------------------------------------------------------------------- #


def _worker_main(requests: Any, responses: Any, logs: Any, boot: Dict[str, Any]) -> None:
    from logging.handlers import QueueHandler
    from pathlib import Path

    config_file = boot.get("config_file")
    ctx.setup(
        log_level=boot.get("log_level"),
        config_file=Path(config_file) if config_file else None,
        sync_remote_index=False,
    )
    # every record goes to the main process, which logs it where its own go
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(QueueHandler(logs))
    root.setLevel(int(boot.get("root_level") or logging.WARNING))

    # Read what the app has learned about sites; never write it back over the
    # main process's copy (each write replaces the whole file). What changes
    # here is sent to the main process instead.
    from ...core import scraping

    scraping.memory().path = None
    sent: Dict[str, dict] = scraping.profile_snapshot()
    sent_lock = Lock()

    def send_profiles() -> None:
        nonlocal sent
        with sent_lock:
            now = scraping.profile_snapshot()
            changed = {key: value for key, value in now.items() if sent.get(key) != value}
            if changed:
                responses.put(("profiles", changed))
            sent = now

    stopping = Event()

    def sync_profiles() -> None:
        while not stopping.wait(PROFILE_SYNC_SECONDS):
            try:
                send_profiles()
            except Exception:
                logger.debug("Could not send site profiles", exc_info=True)

    Thread(target=sync_profiles, name="lncrawl-profile-sync", daemon=True).start()

    signals: Dict[int, Event] = {}
    lock = Lock()
    threads: List[Thread] = []

    def execute(request_id: int, job_ids: List[str], concurrency: int, signal: Event) -> None:
        error: Optional[str] = None
        try:
            _run_jobs(job_ids, concurrency, signal)
        except BaseException as e:  # reported to the main process, which falls back
            error = repr(e)
        finally:
            with lock:
                signals.pop(request_id, None)
            try:
                send_profiles()  # before "done", so the main process has them first
            except Exception:
                logger.debug("Could not send site profiles", exc_info=True)
            responses.put(("done", request_id, error))

    while True:
        message = requests.get()
        kind = message[0]
        if kind == "run":
            _, request_id, job_ids, concurrency = message
            signal = Event()
            with lock:
                signals[request_id] = signal
            thread = Thread(
                target=execute,
                args=(request_id, job_ids, concurrency, signal),
                daemon=True,
            )
            thread.start()
            threads.append(thread)
        elif kind == "cancel":
            with lock:
                pending = signals.get(message[1])
            if pending is not None:
                pending.set()
        elif kind == "stop":
            with lock:
                for pending in signals.values():
                    pending.set()
            break

    for thread in threads:
        thread.join(STOP_SECONDS)
    stopping.set()
    try:
        send_profiles()
    except Exception:
        pass
    ctx.destroy()


def _run_jobs(job_ids: List[str], concurrency: int, signal: Event) -> None:
    from ...exceptions import AbortedException
    from .handlers import run_job, run_jobs_concurrently

    jobs = []
    for job_id in job_ids:
        job = ctx.jobs.get(job_id)
        if not job.is_done:  # e.g. cancelled on its own since it was handed over
            jobs.append((job, signal))
    try:
        if len(jobs) == 1:
            # one job (an export): run as is, without the batch's stall guard,
            # which would stop a job that rightly runs for hours
            run_job(*jobs[0])
            return
        run_jobs_concurrently(jobs, concurrency, signal)
    except AbortedException:
        pass  # cancelled: the jobs say how far they got
