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

    def finish(self, pace: PaceController, seconds: float, per_second: float, slow_every: int = 0):
        """Requests finishing at ``per_second`` for ``seconds``; every Nth one times out."""
        timeout = CrawlTimeoutError("Chapter request timed out after 60 seconds")
        for i in range(int(seconds * per_second)):
            self.clock.now += 1 / per_second
            pace.report(timeout if slow_every and i % slow_every == 0 else None)

    def test_an_odd_slow_page_no_longer_holds_the_speed_down(self) -> None:
        # what the NovelFire export did: about 1 request in 20 timed out, and
        # the rate sat near 0.7/s for days
        pace = self.pace(0.7)
        self.finish(pace, 600, 2, slow_every=20)
        self.assertEqual(pace.rate, export_pace.RPS_MAX)
        self.assertEqual(pace.slowdowns(), {})

    def test_a_site_where_many_requests_time_out_is_eased_off(self) -> None:
        pace = self.pace(2.0)
        self.finish(pace, 31, 2, slow_every=2)  # half of them time out
        self.assertEqual(pace.rate, 1.7)
        self.assertEqual(pace.slowdowns(), {"slow": 1})

    def test_broken_pages_change_nothing(self) -> None:
        pace = self.pace(2.0)
        for _ in range(50):
            self.clock.now += 1
            pace.report(ValueError("Chapter has no content"))
        self.assertEqual(pace.rate, 2.0)

    def test_speed_comes_back_with_successes(self) -> None:
        pace = self.pace(0.2)
        self.finish(pace, 1200, 1)
        self.assertEqual(pace.rate, export_pace.RPS_MAX)
        self.assertGreater(len(self.changes), 5)

    def test_speed_comes_back_from_the_floor_at_the_floors_own_pace(self) -> None:
        # The limiter lets requests through only as fast as the rate, so at the
        # floor (one every 5s) a 30s window never held the 10 it needed to be
        # judged: the NovelFire export sat at 0.2 req/s with nothing failing,
        # and the remembered rate started every later run there too.
        pace = self.pace(export_pace.RPS_MIN)
        deadline = self.clock.now + 3600
        while self.clock.now < deadline and pace.rate < export_pace.RPS_MAX:
            self.clock.now += 1 / pace.rate
            pace.report()
        self.assertEqual(pace.rate, export_pace.RPS_MAX)
        self.assertEqual(pace.slowdowns(), {})

    def test_a_refusal_is_reported_as_the_reason_for_a_slowdown(self) -> None:
        pace = self.pace(2.0)
        pace.report(refused())
        self.assertEqual(pace.slowdowns(), {"refused": 1})
        self.clock.now += export_pace.CUTS_WINDOW + 1
        self.assertEqual(pace.slowdowns(), {})

    def test_never_faster_than_the_rate_asked_for(self) -> None:
        pace = self.pace(1.0, ceiling=1.5)
        self.finish(pace, 600, 1)
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


class RatePlanTests(unittest.TestCase):
    """Where a run's request rate starts, and the fastest it may go."""

    def plan(self, extra: dict, remembered: float = 0.0):
        from lncrawl.services.scheduler.handlers.export_source import _rate_plan

        return _rate_plan(extra, remembered)

    def test_a_resumed_job_is_not_capped_at_the_rate_the_tuner_had_reached(self) -> None:
        # The tuner used to write its rate over the one asked for, and a resume
        # read it back as the cap: an export slowed to 0.2 req/s resumed unable
        # to go any faster (NovelFire, at 4 chapters a minute).
        stale = {"auto_tune": True, "requests_per_sec": 0.2, "found_by_discovery": True}
        self.assertEqual(self.plan(stale), (0.2, export_pace.RPS_MAX, True))

    def test_a_rate_that_was_asked_for_stays_the_cap(self) -> None:
        asked = {"auto_tune": True, "requests_per_sec": 1.0}
        self.assertEqual(self.plan(asked), (1.0, 1.0, False))
        # resumed after the split: carries on where it got to, still capped
        resumed = {**asked, "found_by_discovery": True, "current_rps": 0.6}
        self.assertEqual(self.plan(resumed), (0.6, 1.0, False))

    def test_a_fixed_rate_is_never_reread_as_stale(self) -> None:
        fixed = {"auto_tune": False, "requests_per_sec": 0.5, "found_by_discovery": True}
        self.assertEqual(self.plan(fixed), (0.5, 0.5, False))

    def test_an_auto_job_starts_where_it_or_the_site_last_got_to(self) -> None:
        self.assertEqual(self.plan({"auto_tune": True}, remembered=1.5), (1.5, 5.0, False))
        self.assertEqual(self.plan({"polite": True}), (1.0, 5.0, False))
        resumed = {"found_by_discovery": True, "current_rps": 3.0}
        self.assertEqual(self.plan(resumed, remembered=1.5), (3.0, 5.0, False))


class ImagePacingTests(unittest.TestCase):
    """Whether a run's page images wait for the page rate or only their host."""

    def unpaced(self, extra: dict) -> bool:
        from lncrawl.services.scheduler.handlers.export_source import _images_unpaced

        return _images_unpaced(extra)

    def test_left_to_their_host_when_the_app_chooses_the_speed(self) -> None:
        self.assertTrue(self.unpaced({}))
        self.assertTrue(self.unpaced({"auto_tune": True, "current_rps": 2.5}))
        # an older job, whose stored rate is only where the tuner had got to
        stale = {"auto_tune": True, "requests_per_sec": 0.2, "found_by_discovery": True}
        self.assertTrue(self.unpaced(stale))

    def test_held_to_the_rate_when_the_user_asked_for_one(self) -> None:
        self.assertFalse(self.unpaced({"polite": True}))
        self.assertFalse(self.unpaced({"requests_per_sec": 2, "current_rps": 2}))
        self.assertFalse(self.unpaced({"auto_tune": False, "requests_per_sec": 3}))


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

    def test_the_browser_component_can_be_imported(self) -> None:
        # nodriver 0.50.3 to 0.50.5 shipped cdp/network.py with a byte that is
        # not UTF-8, so Python refused to import it at all: every source that
        # opens pages in a browser, and every bot check, failed in the app.
        from lncrawl import webdriver

        if not webdriver.BROWSER_PAGES_SUPPORTED:
            self.skipTest("nodriver needs Python 3.10 or newer")
        webdriver._force_import_cdp()


if __name__ == "__main__":
    unittest.main()
