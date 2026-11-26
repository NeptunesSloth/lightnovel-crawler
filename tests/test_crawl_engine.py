import asyncio
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event
import unittest

from lncrawl.core.request_budget import (
    CrawlTimeoutError,
    current_budget,
    request_budget,
    waiting_for,
    waiting_turn,
)
from lncrawl.exceptions import AbortedException
from lncrawl.services.crawl_engine import CrawlStalledError, crawl_items
from lncrawl.utils.async_loop import run_async


class CrawlEngineTests(unittest.TestCase):
    def test_repeated_source_failures_stop_scheduling_but_drain_inflight_work(self) -> None:
        submitted = []

        def submit(item):
            submitted.append(item)
            future = Future()
            future.set_exception(TimeoutError("Source unavailable"))
            return future

        outcomes = list(
            crawl_items(
                (str(i) for i in range(10000)),
                submit,
                Event(),
                max_inflight=4,
            )
        )
        self.assertLessEqual(len(submitted), 14)
        self.assertEqual(len(outcomes), len(submitted))
        self.assertTrue(all(outcome.error is not None for outcome in outcomes))

    def test_queue_is_bounded_and_failure_does_not_stop_other_chapters(self) -> None:
        submitted = []
        pending = []

        def submit(item):
            submitted.append(item)
            future = Future()
            pending.append(future)
            if item == "bad":
                future.set_exception(TimeoutError("chapter timed out"))
            else:
                future.set_result(item)
            return future

        stream = crawl_items(["first", "bad", "third", "fourth"], submit, Event(), max_inflight=2)
        first = next(stream)
        self.assertEqual(len(submitted), 2)
        outcomes = [first, *stream]
        self.assertEqual(len(outcomes), 4)
        self.assertEqual(sum(outcome.error is not None for outcome in outcomes), 1)
        self.assertEqual(
            {outcome.value for outcome in outcomes if outcome.value}, {"first", "third", "fourth"}
        )

    def test_cancel_does_not_schedule_remaining_catalogue(self) -> None:
        signal = Event()
        submitted = []

        def submit(item):
            submitted.append(item)
            result = Future()
            result.set_result(item)
            return result

        stream = crawl_items((str(i) for i in range(10000)), submit, signal, max_inflight=4)
        next(stream)
        signal.set()
        with self.assertRaises(AbortedException):
            list(stream)
        self.assertEqual(len(submitted), 4)

    def test_request_budgets_are_isolated_between_worker_threads(self) -> None:
        parent = Event()

        def operation(seconds):
            async def wait():
                await asyncio.sleep(0.05)
                return "complete"

            with request_budget(parent, seconds):
                try:
                    return run_async(wait())
                except CrawlTimeoutError:
                    return "timed out"

        with ThreadPoolExecutor(max_workers=2) as workers:
            short = workers.submit(operation, 0.01)
            long = workers.submit(operation, 1)
            self.assertEqual(short.result(), "timed out")
            self.assertEqual(long.result(), "complete")
        self.assertFalse(parent.is_set())

    def test_cancellation_and_timeout_remain_distinct(self) -> None:
        signal = Event()
        with request_budget(signal, 1) as budget:
            signal.set()
            with self.assertRaises(AbortedException):
                budget.remaining()


class StallWatchdogTests(unittest.TestCase):
    """A batch queued for the site's own pace is not a hang; a hung one still is."""

    def run_one(self, wait_inside_turn: bool, seconds: float, wall: float) -> list:
        signal, release = Event(), Event()
        pool = ThreadPoolExecutor(1)
        self.addCleanup(pool.shutdown)
        self.addCleanup(release.set)

        def fetch(item: str) -> str:
            with request_budget(signal):
                if wait_inside_turn:
                    with waiting_turn():  # its turn with the site has not come yet
                        release.wait(seconds)
                else:
                    release.wait(seconds)  # e.g. a read that never returns
            return item

        return list(
            crawl_items(
                ["chapter"],
                lambda item: pool.submit(fetch, item),
                signal,
                idle_timeout=0.2,
                waiting=lambda: waiting_for(signal),
                wall=wall,
            )
        )

    def test_waiting_for_its_turn_longer_than_the_idle_timeout_is_not_a_stall(self) -> None:
        outcomes = self.run_one(wait_inside_turn=True, seconds=0.7, wall=5)
        self.assertEqual([outcome.value for outcome in outcomes], ["chapter"])

    def test_nothing_finishing_and_nothing_queued_is_still_a_stall(self) -> None:
        with self.assertRaisesRegex(CrawlStalledError, "0.2 seconds"):
            self.run_one(wait_inside_turn=False, seconds=3, wall=5)

    def test_queued_past_the_wall_is_a_stall(self) -> None:
        with self.assertRaisesRegex(CrawlStalledError, "minutes"):
            self.run_one(wait_inside_turn=True, seconds=3, wall=0.6)

    def test_the_count_is_kept_per_signal_and_goes_back_to_nothing(self) -> None:
        mine, other = Event(), Event()
        with request_budget(mine):
            budget = current_budget()
            assert budget is not None
            with budget.paused():
                with budget.paused():  # two requests of one chapter count once
                    self.assertEqual(waiting_for(mine), 1)
                    self.assertEqual(waiting_for(other), 0)
        self.assertEqual(waiting_for(mine), 0)


if __name__ == "__main__":
    unittest.main()
