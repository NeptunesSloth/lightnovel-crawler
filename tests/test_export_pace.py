import threading
import time
from typing import List, Optional
import unittest
import unittest.mock

from lncrawl.core.request_budget import CrawlTimeoutError, request_budget
from lncrawl.exceptions import AbortedException
from lncrawl.services.scheduler.handlers import export_pace
from lncrawl.services.scheduler.handlers.export_pace import (
    PaceController,
    estimate_left,
    pressure,
)
from lncrawl.services.scheduler.handlers.export_source import ExportStalledError, _run_lanes
from lncrawl.utils.ratelimit import RateLimiter


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeTaskman:
    def __init__(self) -> None:
        self.limiter: Optional[RateLimiter] = None

    def use_limiter(self, limiter: Optional[RateLimiter]) -> None:
        self.limiter = limiter


def refused() -> Exception:
    return Exception("HTTP 429 Too Many Requests")


class PaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.changes: List[float] = []

    def pace(self, rate: float, **kwargs) -> PaceController:
        return PaceController(rate, on_change=self.changes.append, clock=self.clock, **kwargs)

    def test_own_time_limit_is_slowness_not_a_refusal(self) -> None:
        self.assertEqual(pressure("Novel details request timed out after 600 seconds"), "slow")
        self.assertEqual(pressure("HTTPError: 403 Forbidden"), "refused")
        self.assertEqual(pressure("ValueError: no title found"), "")

    def test_a_refusal_halves_the_rate_once_per_burst(self) -> None:
        pace = self.pace(2.0)
        for _ in range(4):  # the requests in flight all fail together
            pace.report(refused())
        self.assertEqual(pace.rate, 1.0)
        self.clock.now += export_pace.CUT_COOLDOWN + 1
        pace.report(refused())
        self.assertEqual(pace.rate, 0.5)

    def test_slowness_only_trims_and_broken_pages_change_nothing(self) -> None:
        pace = self.pace(2.0)
        pace.report(CrawlTimeoutError("Chapter request timed out after 60 seconds"))
        self.assertEqual(pace.rate, 1.7)
        self.clock.now += 100
        pace.report(ValueError("Chapter has no content"))
        self.assertEqual(pace.rate, 1.7)

    def test_speed_comes_back_with_successes(self) -> None:
        pace = self.pace(0.2)
        for _ in range(export_pace.RAISE_AFTER * 20):
            pace.report()
        self.assertEqual(pace.rate, export_pace.RPS_MAX)
        self.assertGreater(len(self.changes), 5)

    def test_never_faster_than_the_rate_asked_for(self) -> None:
        pace = self.pace(1.0, ceiling=1.5)
        for _ in range(export_pace.RAISE_AFTER * 10):
            pace.report()
        self.assertEqual(pace.rate, 1.5)

    def test_fixed_rate_is_left_alone(self) -> None:
        pace = self.pace(1.0, auto=False)
        pace.report(refused())
        for _ in range(100):
            pace.report()
        self.assertEqual(pace.rate, 1.0)

    def test_an_uncapped_run_gets_a_rate_on_its_first_refusal_for_every_lane(self) -> None:
        pace = self.pace(0)
        first, second = FakeTaskman(), FakeTaskman()
        pace.attach(first)
        pace.attach(second)
        self.assertIsNone(first.limiter)
        pace.report(refused())
        self.assertEqual(pace.rate, export_pace.RPS_UNCAPPED_START)
        self.assertIsNotNone(first.limiter)
        self.assertIs(first.limiter, second.limiter)

    def test_lanes_share_one_limiter_whose_rate_moves_in_place(self) -> None:
        pace = self.pace(2.0)
        first, second = FakeTaskman(), FakeTaskman()
        pace.attach(first)
        pace.attach(second)
        limiter = first.limiter
        self.assertIs(limiter, second.limiter)
        pace.report(refused())
        self.assertIs(first.limiter, limiter)
        assert limiter is not None
        self.assertAlmostEqual(limiter.period, 1.0)

    def test_time_left_from_the_recent_pace(self) -> None:
        pace = self.pace(0)
        self.assertEqual(pace.pace(), 0.0)
        for _ in range(60):
            self.clock.now += 1
            pace.report()
        self.assertAlmostEqual(pace.pace(), 1.0, places=1)
        # 50 left in the novels open now, 2 more novels of about 100 each
        left = estimate_left(pace.pace(), [(50, 100), (0, 0)], 2, 100)
        assert left is not None
        self.assertAlmostEqual(left, 250, delta=10)
        self.assertIsNone(estimate_left(0.0, [(0, 10)], 1, 10))


class LaneTests(unittest.TestCase):
    def test_novels_run_side_by_side(self) -> None:
        inside = []
        most = [0]
        lock = threading.Lock()

        def lane(index: int) -> None:
            with lock:
                inside.append(index)
                most[0] = max(most[0], len(inside))
            time.sleep(0.2)
            with lock:
                inside.remove(index)

        _run_lanes(2, lane)
        self.assertEqual(most[0], 2)

    def test_a_stall_is_reported_over_the_other_lanes_stopping(self) -> None:
        def lane(index: int) -> None:
            if index == 0:
                raise AbortedException()
            time.sleep(0.05)
            raise ExportStalledError("No downloads completed")

        with self.assertRaises(ExportStalledError):
            _run_lanes(2, lane)


class LimiterTests(unittest.TestCase):
    def test_rate_can_change_while_in_use(self) -> None:
        limiter = RateLimiter(1.0)
        limiter.set_rate(4.0)
        self.assertAlmostEqual(limiter.period, 0.25)
        with self.assertRaises(ValueError):
            limiter.set_rate(0)

    def test_novel_details_time_limit_says_what_timed_out(self) -> None:
        with self.assertRaises(CrawlTimeoutError) as caught:
            with request_budget(threading.Event(), 0.01, label="Novel details request") as b:
                time.sleep(0.02)
                b.remaining()
        self.assertIn("Novel details request timed out", str(caught.exception))


class BrowserGuardTests(unittest.TestCase):
    def test_browser_pages_off_cleanly_on_old_python(self) -> None:
        from lncrawl import webdriver

        with unittest.mock.patch.object(webdriver, "BROWSER_PAGES_SUPPORTED", False):
            with self.assertRaises(RuntimeError) as caught:
                webdriver.create_new()
            self.assertIn("Python 3.10", str(caught.exception))
            webdriver.warmup_nodriver()  # a no-op, not an import crash


if __name__ == "__main__":
    unittest.main()
