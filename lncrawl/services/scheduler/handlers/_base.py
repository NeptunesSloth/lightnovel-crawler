from abc import ABC, abstractmethod
from functools import cached_property
from threading import Event
import traceback
from typing import Any, Callable, Dict, List, Optional, Tuple

from ....context import ctx
from ....dao import Job, JobStatus
from ....exceptions import AbortedException
from ....utils.time_utils import current_timestamp


class HandlerException(Exception):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


class BaseHandler(ABC):
    def __init__(
        self,
        job: Job,
        signal: Optional[Event] = None,
    ) -> None:
        self.job = job
        self.signal = signal or Event()

    @staticmethod
    @abstractmethod
    def can_activate(job: Job) -> bool:
        raise NotImplementedError()

    @abstractmethod
    def run(self) -> None:
        raise NotImplementedError()

    def process(self) -> bool:
        self._log_entry()
        try:
            self.run()
            return self._set_success()
        except AbortedException:
            return False  # ignore error
        except HandlerException as e:
            return self._set_failure(e.message, e)
        except Exception as e:
            return self._set_failure("Failed to create requests", e)

    # ------------------------------------------------------------------ #
    #                               Helpers                              #
    # ------------------------------------------------------------------ #

    @cached_property
    def user(self):
        return ctx.users.get(self.job.user_id)

    def _log_entry(self):
        message = f"[cyan]{self.job.status.name}[/cyan] [b]{self.job.id}[/b] | {self.job.job_title}"
        if not self.job.parent_job_id:
            ctx.logger.info(message)
        else:
            ctx.logger.debug(message)

    def _set_extra(self, **values: Any) -> None:
        ctx.jobs.update_extra(self.job, values)

    def _set_running(self) -> None:
        with ctx.db.session() as sess:
            now = current_timestamp()
            ctx.jobs._update(
                sess,
                self.job.id,
                started_at=now,
                status=JobStatus.RUNNING,
            )
            sess.commit()
            self.job.started_at = now
            self.job.status = JobStatus.RUNNING

        ctx.job_notifier.notify(self.user, self.job)

    def _increment(self) -> bool:
        with ctx.db.session() as sess:
            ctx.jobs._increment_up(sess, self.job.id)
            sess.commit()
            self.job = sess.get_one(Job, self.job.id)

        ctx.job_notifier.notify(self.user, self.job)
        return True

    def _set_success(self) -> bool:
        with ctx.db.session() as sess:
            ctx.jobs._success(sess, self.job.id)
            sess.commit()
            self.job = sess.get_one(Job, self.job.id)

        ctx.job_notifier.notify(self.user, self.job)
        return True

    def _set_failure(self, error: str = "", err_source: Optional[Exception] = None) -> bool:
        if error and err_source:
            lines = traceback.format_exception(
                type(err_source),
                value=err_source,
                tb=err_source.__traceback__,
                chain=True,
            )
            lines += ["", error]
            error = "".join(lines)

        with ctx.db.session() as sess:
            ctx.jobs._fail(sess, self.job.id, error.strip())
            sess.commit()
            self.job = sess.get_one(Job, self.job.id)

        ctx.job_notifier.notify(self.user, self.job)
        return False


class BatchHandler(BaseHandler):
    def __init__(
        self,
        job: Job,
        signal: Optional[Event] = None,
    ) -> None:
        self.children: List[Job] = []
        super().__init__(job, signal)

    def process(self) -> bool:
        self._log_entry()

        if self.job.is_running:
            self.children = ctx.jobs.get_children(self.job.id)
            for job in self.children:
                if not job.is_done:
                    break
            else:
                return self._set_success()

        try:
            self.run()
            return self._increment()
        except AbortedException:
            return False  # ignore error
        except HandlerException as e:
            return self._set_failure(e.message, e)
        except Exception as e:
            return self._set_failure("Failed to create requests", e)

    def _run_children_inline(
        self,
        key: str,
        keys: List[str],
        create: Callable[[str], Job],
    ) -> None:
        """Run one child job per key here, a few at a time, instead of queueing them.

        Each child is still a job of its own, so its progress, failure and
        cancellation show and count exactly as before. But a worker no longer has
        to find each one in the database, and the children share a few pooled
        crawlers: at the same request rate this ran about three times as many
        chapters per second as leaving them to the job queue.

        Children an earlier run of this job made (``extra[key]`` names their key)
        are reused rather than made again, so a batch resumed after a restart
        carries on where it was.
        """
        from ....core.crawler import DEFAULT_WORKERS
        from ....core.taskman import TaskManager
        from ....services.crawl_engine import crawl_items
        from ..runner import JobRunner
        from . import run_job

        # Make the missing children outside the scheduler's lock, which every
        # worker needs to pick a job; a worker that takes one of them meanwhile
        # simply runs it, and the claim below leaves it to that worker.
        known = {child.extra.get(key) for child in ctx.jobs.get_children(self.job.id)}
        for item in keys:
            if self.signal.is_set():
                raise AbortedException()
            if item not in known:
                known.add(item)
                create(item)

        def runnable() -> List[Job]:
            # The same jobs a worker would pick: never started, or started and
            # interrupted before counting anything. Running one that already
            # counted progress again would count it twice.
            return [
                child
                for child in ctx.jobs.get_children(self.job.id)
                if child.status == JobStatus.PENDING
                or (child.status == JobStatus.RUNNING and child.done == 0)
            ]

        claimed = JobRunner.claim(runnable)
        jobs: Dict[str, Tuple[Job, Event]] = {job.id: (job, sig) for job, sig in claimed}

        def run_child(job_id: str) -> bool:
            job, job_signal = jobs[job_id]
            if self.signal.is_set():
                job_signal.set()
            return run_job(job, job_signal)

        taskman = TaskManager(workers=DEFAULT_WORKERS, signal=self.signal)
        try:
            for _ in crawl_items(
                list(jobs),
                lambda job_id: taskman.submit_task(run_child, job_id),
                self.signal,
                max_inflight=DEFAULT_WORKERS,
                # Each child bounds its own requests; this only guards a hang.
                idle_timeout=600,
            ):
                pass
        finally:
            taskman.close()
            for job_id, (_, job_signal) in jobs.items():
                JobRunner.release(job_id, job_signal)


class FallbackHandler(BaseHandler):
    @staticmethod
    def can_activate(job) -> bool:
        return True

    def run(self) -> None:
        raise HandlerException(f"Job type is not supported: {self.job.type!r}")
