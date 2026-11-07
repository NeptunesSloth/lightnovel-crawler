"""Idle job runners sleep until woken instead of polling the database."""

from threading import Event, Thread
import time
import unittest
from unittest.mock import MagicMock, patch

import test_novel_repair

from lncrawl.enums import JobType
from lncrawl.services.scheduler import runner, service
from lncrawl.utils import wakeup as wakeup_module
from lncrawl.utils.event_lock import EventLock
from lncrawl.utils.wakeup import Wakeup


class WakeupTests(unittest.TestCase):
    def test_a_ring_before_anyone_waits_is_kept(self) -> None:
        bell = Wakeup()
        bell.ring()
        start = time.monotonic()
        self.assertTrue(bell.wait(5))
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertFalse(bell.wait(0.01))

    def test_a_burst_of_rings_is_capped(self) -> None:
        bell = Wakeup(limit=3)
        for _ in range(100):
            bell.ring()
        self.assertEqual(sum(bell.wait(0.01) for _ in range(10)), 3)

    def test_a_ring_wakes_a_waiter(self) -> None:
        bell = Wakeup()
        woke = Event()
        Thread(target=lambda: bell.wait(5) and woke.set(), daemon=True).start()
        time.sleep(0.05)
        bell.ring()
        self.assertTrue(woke.wait(1))


class SchedulerWakeupTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = test_novel_repair.NovelRepairTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.ctx, self.user, self.stack = fixture.ctx, fixture.user, fixture.stack
        for module in (runner, service):
            self.stack.enter_context(patch.object(module, "ctx", self.ctx))
        for name in ("_queue", "_users", "_types", "_sites"):
            self.stack.enter_context(patch.object(runner, name, {}))
        self.stack.enter_context(patch.object(runner, "_lock", EventLock()))
        # fresh bells, shared by every module that rings or waits on them
        bells = {"job_wakeup": Wakeup(), "artifact_wakeup": Wakeup()}
        for module in (wakeup_module, runner, service):
            for name, bell in bells.items():
                self.stack.enter_context(patch.object(module, name, bell))

    def test_a_new_job_starts_without_waiting_for_the_idle_poll(self) -> None:
        started = Event()
        self.stack.enter_context(
            patch.object(runner, "run_job", side_effect=lambda job, signal: started.set())
        )
        self.stack.enter_context(patch.object(service, "run_scrubber", MagicMock()))
        self.stack.enter_context(patch("lncrawl.webdriver.warmup_nodriver"))
        config = self.ctx.config.crawler
        config.runner_concurrency = 2
        config.runner_cooldown = 1
        config.scrubber_cooldown = 60
        scheduler = service.JobScheduler()
        scheduler.start()
        try:
            time.sleep(0.3)  # every runner has looked once and gone to sleep
            created = time.monotonic()
            self.ctx.jobs._create(
                self.user,
                JobType.EXPORT_SOURCE,
                {"domain": "novelfire.net", "urls": ["https://novelfire.net/book/example"]},
            )
            self.assertTrue(started.wait(2), "the new job was not picked up")
            # well inside the 5 s idle poll and the 1 s cooldown it replaced
            self.assertLess(time.monotonic() - created, 0.5)
        finally:
            scheduler.stop()


if __name__ == "__main__":
    unittest.main()
