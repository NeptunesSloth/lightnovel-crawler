import logging
from threading import Event
from typing import Callable, Dict, Iterable, List, Tuple

from ...context import ctx
from ...dao import Job
from ...enums import JobType
from ...utils.event_lock import EventLock
from .handlers import run_job

logger = logging.getLogger(__name__)

_lock = EventLock()
_queue: Dict[str, Event] = {}
_users: Dict[str, str] = {}
_types: Dict[str, JobType] = {}
_sites: Dict[str, str] = {}  # export job id -> its site, while a worker holds it


def held_export_sites() -> List[str]:
    """Sites a bulk export is downloading in this process right now."""
    return [site for site in list(_sites.values()) if site]


class JobRunner:
    @staticmethod
    def run(signal: Event, artifact: bool) -> bool:
        """Claim and run the next pending job. True if one was taken off the queue."""
        with _lock.using(signal):
            # get next pending job for a user that doesn't have any running job
            # exports still held by a worker count as running even once
            # cancelled, until that worker has actually stopped
            held = list(_sites.items())
            job = ctx.jobs._pending(
                artifact,
                skip_job_ids=_queue.keys(),
                skip_user_ids=_users.values(),
                held_exports=held,
            )
            # if no such job, get next pending job for any existing users
            if not job:
                job = ctx.jobs._pending(
                    artifact,
                    skip_job_ids=_queue.keys(),
                    held_exports=held,
                )
            if not job:
                return False  # no pending job

            # if it is an internal job, cancel it if it is part of a finished job
            if job.parent_job_id:
                if ctx.jobs.cancel_if_dangling(job):
                    logger.debug(f"Dangling job [b]{job.id}[/b] | {job.job_title}")
                    return True

            # add the job to queue
            job_signal = Event()
            _queue[job.id] = job_signal
            _users[job.id] = job.user_id
            _types[job.id] = job.type
            if job.type == JobType.EXPORT_SOURCE:
                _sites[job.id] = str((job.extra or {}).get("domain") or "")

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
                    _sites.pop(job.id, None)
        return True

    @staticmethod
    def claim(load: Callable[[], Iterable[Job]]) -> List[Tuple[Job, Event]]:
        """Take jobs for the caller to run itself, so no worker picks them up.

        ``load`` runs under the same lock workers hold while choosing a job. It
        should create any new jobs and re-read existing ones there: a job no
        worker holds and that is not done can then be run by no one else. Each
        claimed job gets its own signal, set by `cancel` like a worker's, and
        must be given back with `release` once it has run.
        """
        claimed: List[Tuple[Job, Event]] = []
        with _lock:
            for job in load():
                if job.is_done or job.id in _queue:
                    continue
                job_signal = Event()
                _queue[job.id] = job_signal
                _users[job.id] = job.user_id
                _types[job.id] = job.type
                claimed.append((job, job_signal))
        return claimed

    @staticmethod
    def reserve(job_ids: Iterable[str], user_id: str, job_type: JobType) -> Dict[str, Event]:
        """Claim jobs before they exist, so no worker can take them once they do.

        For a caller about to create jobs with these ids and run them itself.
        Give each back with `release`, whether or not it was created.
        """
        reserved: Dict[str, Event] = {}
        with _lock:
            for job_id in job_ids:
                if job_id in _queue:
                    continue
                job_signal = Event()
                _queue[job_id] = job_signal
                _users[job_id] = user_id
                _types[job_id] = job_type
                reserved[job_id] = job_signal
        return reserved

    @staticmethod
    def release(job_id: str, job_signal: Event) -> None:
        """Give back a job taken with `claim`, stopping it if it still runs."""
        with _lock:
            if _queue.get(job_id) is job_signal:
                _queue.pop(job_id).set()
                _users.pop(job_id, None)
                _types.pop(job_id, None)

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
