from abc import ABC, abstractmethod
from functools import cached_property
from threading import Event
import traceback
from typing import Any, List, Optional

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


class FallbackHandler(BaseHandler):
    @staticmethod
    def can_activate(job) -> bool:
        return True

    def run(self) -> None:
        raise HandlerException(f"Job type is not supported: {self.job.type!r}")
