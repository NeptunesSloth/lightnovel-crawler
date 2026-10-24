import logging
from threading import Event
from typing import Dict

from ...context import ctx
from ...enums import JobType
from ...utils.event_lock import EventLock
from .handlers import run_job

logger = logging.getLogger(__name__)

_lock = EventLock()
_queue: Dict[str, Event] = {}
_users: Dict[str, str] = {}
_types: Dict[str, JobType] = {}


class JobRunner:
    @staticmethod
    def run(signal: Event, artifact: bool):
        with _lock.using(signal):
            # get next pending job for a user that doesn't have any running job
            job = ctx.jobs._pending(
                artifact,
                skip_job_ids=_queue.keys(),
                skip_user_ids=_users.values(),
                skip_exports=JobType.EXPORT_SOURCE in _types.values(),
            )
            # if no such job, get next pending job for any existing users
            if not job:
                job = ctx.jobs._pending(
                    artifact,
                    skip_job_ids=_queue.keys(),
                    skip_exports=JobType.EXPORT_SOURCE in _types.values(),
                )
            if not job:
                return  # no pending job

            # if it is an internal job, cancel it if it is part of a finished job
            if job.parent_job_id:
                if ctx.jobs.cancel_if_dangling(job):
                    logger.debug(f"Dangling job [b]{job.id}[/b] | {job.job_title}")
                    return

            # add the job to queue
            job_signal = Event()
            _queue[job.id] = job_signal
            _users[job.id] = job.user_id
            _types[job.id] = job.type

        # process the current job
        try:
            run_job(job, job_signal)
        finally:
            # release from queue
            with _lock:
                if _queue.get(job.id) is job_signal:
                    _queue.pop(job.id).set()
                    _users.pop(job.id, None)
                    _types.pop(job.id, None)

    @staticmethod
    def cancel(job_id: str, cancel_children: bool = True):
        # Keep the lease until the worker exits. Cancellation is a request, not
        # evidence that its network/browser operations have already stopped.
        with _lock:
            if job_id in _queue:
                _queue[job_id].set()

        # recursively cancel all children as well
        if cancel_children:
            for job_id in ctx.jobs.get_children_ids(job_id):
                JobRunner.cancel(job_id)

    @staticmethod
    def cancel_all():
        # The owning workers release their leases in finally.
        with _lock:
            for signal in _queue.values():
                signal.set()
