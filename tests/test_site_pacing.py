"""Pacing against a site that allows so many requests per time window.

NovelFire's chapter mirror allows a handful of requests every ten seconds and
answers the rest with 429 and "Retry-After: 10", the time until its window
reopens. The scraper library took that number as the new gap between every
request, and re-read the slowed-down gap from disk before each fetch, so one
refusal held the site at a request per ten seconds for the rest of the run and
the runs after it.
"""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import math
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
from typing import Iterator, List
import unittest

from scraper import PacingPolicy, Scraper, ScraperConfig
from scraper.memory import Memory

from lncrawl.core import scraping
from lncrawl.core.request_budget import (
    CrawlTimeoutError,
    current_budget,
    request_budget,
)
from lncrawl.core.scraping import CARRY_OVER_MAX, SitePacer, pacing_for, site_state
from lncrawl.utils.ratelimit import RateLimiter


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class SitePacerTests(unittest.TestCase):
    def test_retry_after_is_a_wait_not_the_new_pace(self) -> None:
        pacer = SitePacer(pacing_for(0))
        widened = pacer.throttled("site", retry_after=10)
        self.assertLess(widened, 1.0)

    def test_one_refused_burst_widens_once(self) -> None:
        clock = Clock()
        pacer = SitePacer(PacingPolicy(interval=0.5, floor=0.05), clock=clock)
        first = pacer.throttled("site", retry_after=10)
        self.assertEqual(first, 1.0)
        # the other requests in flight are refused by the same closed window
        for _ in range(3):
            clock.now += 1
            self.assertEqual(pacer.throttled("site", retry_after=10), first)
        # once the window has reopened, being refused again means still too fast
        clock.now += 10
        self.assertEqual(pacer.throttled("site", retry_after=10), 2.0)

    def test_easing_is_not_undone_by_rereading_the_profile(self) -> None:
        pacer = SitePacer(pacing_for(0))
        pacer.learn("site", 1.6)  # what an earlier run remembered
        for _ in range(100):
            pacer.learn("site", 1.6)  # every fetch re-reads the saved profile
            pacer.eased("site")
        self.assertLess(pacer.interval_for("site"), 1.0)

    def test_a_remembered_slowdown_is_only_a_starting_point(self) -> None:
        pacer = SitePacer(pacing_for(0))
        pacer.learn("site", 10.0)
        self.assertEqual(pacer.interval_for("site"), CARRY_OVER_MAX)

    def test_never_faster_than_the_rate_asked_for(self) -> None:
        pacer = SitePacer(pacing_for(0.2))  # one request per 5s
        pacer.learn("site", 0.5)
        self.assertEqual(pacer.interval_for("site"), 5.0)
        pacer.learn("other", 30.0)
        self.assertEqual(pacer.interval_for("other"), 5.0)


class _WindowedSite(BaseHTTPRequestHandler):
    """Allows LIMIT requests per WINDOW seconds; the rest get 429 + Retry-After."""

    LIMIT = 6
    WINDOW = 2.0
    lock = threading.Lock()
    window_start = 0.0
    used = 0
    served: List[float] = []

    def do_GET(self) -> None:
        cls = type(self)
        with cls.lock:
            now = time.monotonic()
            if now - cls.window_start >= cls.WINDOW:
                cls.window_start, cls.used = now, 0
            allowed = cls.used < cls.LIMIT
            if allowed:
                cls.used += 1
                cls.served.append(now)
            wait = max(1, math.ceil(cls.window_start + cls.WINDOW - now))
        if not allowed:
            self.send_response(429)
            self.send_header("Retry-After", str(wait))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = f"<html><body><p>{self.path} {'text ' * 50}</p></body></html>".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


class WindowedSiteTests(unittest.TestCase):
    def setUp(self) -> None:
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
        _WindowedSite.served = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _WindowedSite)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.origin = f"http://127.0.0.1:{self.server.server_port}/"

    def test_a_windowed_limit_is_followed_instead_of_slept_through(self) -> None:
        config = ScraperConfig(
            pacing=pacing_for(0),
            data_dir=Path(self.directory.name),
            raise_for_status=False,
        )
        state = site_state(config, Memory(config.memory_path))
        scraper = Scraper(origin=self.origin, config=config, state=state)
        self.addCleanup(scraper.close)
        for page in range(18):
            response = scraper.fetch("GET", f"{self.origin}chapter-{page}")
            self.assertEqual(response.status_code, 200)
        # The site takes 6 every 2 seconds. Taking Retry-After (up to 2s) as the
        # gap leaves it near one request every 2s; stepping up from fast settles
        # within a doubling of the site's 1/3s.
        interval = state.pacer.interval_for(scraper.memory.key(self.origin))
        self.assertLess(interval, 1.0)


@contextmanager
def _slow_gate(seconds: float) -> Iterator[None]:
    time.sleep(seconds)
    yield


class BudgetPauseTests(unittest.TestCase):
    def test_waiting_for_our_own_send_slot_does_not_use_up_the_time_limit(self) -> None:
        limiter = RateLimiter(2.0)  # one slot every 0.5s
        limiter.acquire()  # someone else has the first slot
        with request_budget(threading.Event(), 0.3, label="Novel details request") as budget:
            limiter.acquire()  # waits about 0.5s for its turn
            budget.remaining()  # the site has not been asked anything yet

    def test_the_site_taking_its_time_still_runs_the_limit_out(self) -> None:
        with self.assertRaises(CrawlTimeoutError):
            with request_budget(threading.Event(), 0.05) as budget:
                time.sleep(0.1)
                budget.remaining()

    def test_requests_sent_from_worker_threads_count_against_the_same_limit(self) -> None:
        from lncrawl.core.taskman import TaskManager

        taskman = TaskManager(workers=3, ratelimit=4.0)  # one slot every 0.25s
        self.addCleanup(taskman.close)
        with request_budget(threading.Event(), 0.3, label="Novel details request") as budget:
            futures = [taskman.submit_task(current_budget) for _ in range(4)]
            seen = [future.result(timeout=5) for future in futures]
            # four slots take about 0.75s to come round; none of it was the site's
            budget.remaining()
        self.assertTrue(all(task_budget is budget for task_budget in seen))

    def test_waiting_at_the_scrapers_connection_gate_does_not_use_up_the_limit(self) -> None:
        fake = SimpleNamespace(_paced=lambda key, abort: _slow_gate(0.2))
        scraping._pause_budget_while_paced(fake)
        with request_budget(threading.Event(), 0.1) as budget:
            with fake._paced("site", None):
                budget.remaining()


if __name__ == "__main__":
    unittest.main()
