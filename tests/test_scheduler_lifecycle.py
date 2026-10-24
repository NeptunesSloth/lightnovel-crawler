from threading import Event, Thread
import unittest
from unittest.mock import MagicMock, patch

import test_novel_repair

from lncrawl.enums import JobStatus, JobType
from lncrawl.exceptions import AbortedException
from lncrawl.services.scheduler import runner, service
from lncrawl.utils.event_lock import EventLock


class SchedulerLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = test_novel_repair.NovelRepairTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.ctx = fixture.ctx
        self.user = fixture.user
        self.stack = fixture.stack
        for module in (runner, service):
            self.stack.enter_context(patch.object(module, "ctx", self.ctx))
        for name in ("_queue", "_users", "_types"):
            self.stack.enter_context(patch.object(runner, name, {}))
        self.stack.enter_context(patch.object(runner, "_lock", EventLock()))

    def create_export(self):
        return self.ctx.jobs._create(
            self.user,
            JobType.EXPORT_SOURCE,
            {
                "domain": "novelfire.net",
                "urls": ["https://novelfire.net/book/example"],
            },
        )

    def test_real_scheduler_startup_does_not_cancel_resumed_export(self) -> None:
        export = self.create_export()
        # Reproduce a persisted mid-export snapshot from a previous process.
        with self.ctx.db.session() as session:
            self.ctx.jobs._update(session, export.id, status=JobStatus.RUNNING, done=25, total=50)
            session.commit()
        started, cancelled, release = Event(), Event(), Event()

        def run(job, signal):
            with self.ctx.db.session() as session:
                self.ctx.jobs._update(session, job.id, status=JobStatus.RUNNING, done=25, total=50)
                session.commit()
            started.set()
            if signal.wait(0.2):
                cancelled.set()
            release.wait(2)

        self.stack.enter_context(patch.object(runner, "run_job", side_effect=run))
        self.stack.enter_context(patch.object(service, "run_scrubber", MagicMock()))
        self.stack.enter_context(patch.object(service, "run_artifact_maker", MagicMock()))
        self.stack.enter_context(patch("lncrawl.webdriver.warmup_nodriver"))
        config = self.ctx.config.crawler
        config.runner_concurrency = 1
        config.runner_cooldown = 0.01
        config.scrubber_cooldown = 1
        config.runner_reset_interval = 0.01  # Legacy setting must not kill work.
        scheduler = service.JobScheduler()
        try:
            scheduler.start()
            self.assertTrue(started.wait(1), "Export worker never started")
            self.assertFalse(cancelled.wait(0.3), "Scheduler reset silently cancelled the export")
            self.assertEqual(self.ctx.jobs.get(export.id).status, JobStatus.RUNNING)
            self.assertIn(export.id, runner._queue)
        finally:
            release.set()
            scheduler.stop()

    def test_cancel_keeps_export_lease_until_worker_exits(self) -> None:
        first, second = self.create_export(), self.create_export()
        started, release = Event(), Event()
        calls = []

        def run(job, signal):
            calls.append(job.id)
            if job.id == first.id:
                with self.ctx.db.session() as session:
                    self.ctx.jobs._update(
                        session, job.id, status=JobStatus.RUNNING, done=1, total=2
                    )
                    session.commit()
                started.set()
                release.wait(2)

        self.stack.enter_context(patch.object(runner, "run_job", side_effect=run))
        worker = Thread(target=runner.JobRunner.run, args=(Event(), False))
        worker.start()
        try:
            self.assertTrue(started.wait(1))
            runner.JobRunner.cancel(first.id, cancel_children=False)
            self.ctx.jobs.cancel(first.id)
            self.assertIn(first.id, runner._queue)
            runner.JobRunner.run(Event(), False)
            self.assertEqual(
                calls, [first.id], "A second export started before the cancelled worker stopped"
            )
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertNotIn(first.id, runner._queue)
        runner.JobRunner.run(Event(), False)
        self.assertEqual(calls, [first.id, second.id])

    def test_cancelled_lock_wait_does_not_increase_lock_capacity(self) -> None:
        lock = EventLock()
        cancelled = Event()
        cancelled.set()
        with self.assertRaises(AbortedException):
            with lock.using(cancelled):
                self.fail("Cancelled caller acquired the lock")
        self.assertTrue(lock._sema.acquire(blocking=False))
        self.assertFalse(
            lock._sema.acquire(blocking=False), "Cancelled waiter released a lock it never acquired"
        )


if __name__ == "__main__":
    unittest.main()
