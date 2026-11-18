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
import unittest.mock

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

    def test_a_remembered_slowdown_is_shed_within_a_few_dozen_successes(self) -> None:
        # A run starts at the gap the last one ended on, up to CARRY_OVER_MAX.
        # Ten successes per 10% step took five minutes to climb back to a
        # site's limit; nothing refused means nothing to hold it back for.
        clock = Clock()
        pacer = SitePacer(pacing_for(0), clock=clock)
        pacer.learn("site", CARRY_OVER_MAX)
        for _ in range(45):
            clock.now += pacer.interval_for("site")
            pacer.eased("site")
        self.assertLess(pacer.interval_for("site"), 0.5)

    def test_never_faster_than_the_rate_asked_for(self) -> None:
        pacer = SitePacer(pacing_for(0.2))  # one request per 5s
        pacer.learn("site", 0.5)
        self.assertEqual(pacer.interval_for("site"), 5.0)
        pacer.learn("other", 30.0)
        self.assertEqual(pacer.interval_for("other"), 5.0)

    def test_the_pace_is_worked_out_from_what_the_site_let_through(self) -> None:
        # NovelFire: 10 requests pass, the 11th gets "429, Retry-After: 10". That
        # says ten per ten seconds, so one refusal is enough to find the pace;
        # doubling up from a twentieth of a second took five, 10s blocked each.
        clock = Clock()
        pacer = SitePacer(pacing_for(0), clock=clock)
        for _ in range(10):
            clock.now += 0.2
            pacer.eased("site")
        gap = pacer.throttled("site", retry_after=10)
        self.assertGreater(gap, 1.0)  # under ten per ten seconds
        self.assertLess(gap, 1.3)  # and not much under

    def test_easing_stops_just_short_of_the_pace_that_was_refused(self) -> None:
        clock = Clock()
        pacer = SitePacer(pacing_for(0), clock=clock)

        def succeed(count: int) -> None:
            for _ in range(count):
                clock.now += max(pacer.interval_for("site"), 0.2)
                pacer.eased("site")

        succeed(10)
        pacer.throttled("site", retry_after=10)  # ten in ten seconds was the limit
        clock.now += 10
        succeed(400)
        settled = pacer.interval_for("site")
        self.assertGreater(settled, 1.0)  # never back to the pace that was refused
        self.assertLess(settled, 1.1)  # but right up close to it
        # and it is only remembered for a while, in case the site's limit eased
        clock.now += scraping.TOO_FAST_TTL + 1
        succeed(400)
        self.assertLess(pacer.interval_for("site"), 1.0)

    def test_a_refusal_while_pacing_unevenly_does_not_blame_that_pace(self) -> None:
        # A gap remembered from last run is kept the library's uneven way until
        # the site shows it counts. Uneven gaps trip a counter at a slower
        # average, so that average says nothing about what an even pace may be.
        clock = Clock()
        pacer = SitePacer(pacing_for(0), clock=clock)
        pacer.learn("site", 1.5)
        for _ in range(9):
            clock.now += 1.0  # a burst of short gaps: ten land inside ten seconds
            pacer.eased("site")
        clock.now += 0.5
        pacer.eased("site")
        pacer.throttled("site", retry_after=10)
        clock.now += 10
        for _ in range(400):
            clock.now += pacer.interval_for("site")
            pacer.eased("site")
        self.assertLess(pacer.interval_for("site"), 1.1)  # not held at 1.5

    def test_refused_again_straight_away_backs_off_harder(self) -> None:
        # a site whose window is longer than its Retry-After: what passed in the
        # last few seconds says little, so a quick second refusal doubles
        clock = Clock()
        pacer = SitePacer(pacing_for(0), clock=clock)
        for _ in range(10):
            clock.now += 0.1
            pacer.eased("site")
        first = pacer.throttled("site", retry_after=5)
        clock.now += 5
        for _ in range(8):
            clock.now += first
            pacer.eased("site")
        before = pacer.interval_for("site")
        self.assertAlmostEqual(pacer.throttled("site", retry_after=5), before * 2, places=3)

    def test_refused_again_a_full_window_after_reopening_goes_by_the_count(self) -> None:
        # NovelFire's mirror let 16 through at first, then held to 10 in 10
        # seconds. Each later refusal came a full window after it reopened, so
        # what got through was the site's real count, but it was ignored for a
        # minute after the last refusal and the pace crept up 5% a refusal:
        # eight refusals, each blocking every download for ten seconds.
        clock = Clock()
        pacer = SitePacer(pacing_for(0), clock=clock)
        for _ in range(16):  # the burst it let through at the start
            clock.now += 0.2
            pacer.eased("site")
        pacer.throttled("site", retry_after=10)
        clock.now += 10
        for _ in range(20):  # twenty seconds at the ten in ten it really allows
            clock.now += 1.0
            pacer.eased("site")
        clock.now += 0.5  # the eleventh in its window
        pacer.throttled("site", retry_after=10)
        clock.now += 10
        for _ in range(400):
            clock.now += pacer.interval_for("site")
            pacer.eased("site")
        settled = pacer.interval_for("site")
        self.assertGreater(settled, 1.0)  # under the ten in ten seconds it counted
        self.assertLess(settled, 1.1)

    def test_a_long_wait_holds_the_site_only_for_a_while(self) -> None:
        # A site saying "come back in 4 minutes" held every request to it for
        # the whole 4 minutes. Nothing completed for two, and the export's
        # watchdog stopped a run that finished fine before the pacer held it.
        pacer = SitePacer(pacing_for(0))
        with unittest.mock.patch.object(scraping, "MAX_SITE_BLOCK", 0.3):
            pacer.throttled("site", retry_after=240)
            started = time.monotonic()
            pacer.wait("site")
        self.assertGreaterEqual(time.monotonic() - started, 0.25)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_refusals_during_a_long_closure_do_not_slow_the_site_further(self) -> None:
        # A site closed for 4 minutes is held for one minute at a time, and
        # the requests sent after each minute are refused again. Read as new
        # refusals, each doubled the gap: by the time the site reopened it was
        # sent one request a minute. They are the closure it already named.
        clock = Clock()
        pacer = SitePacer(pacing_for(0), clock=clock)
        for _ in range(10):
            clock.now += 0.2
            pacer.eased("site")
        first = pacer.throttled("site", retry_after=240)
        self.assertLess(first, 1.0)  # a penalty, not a window: no 24s-per-request estimate
        for left in (180, 120, 60):
            clock.now += scraping.MAX_SITE_BLOCK
            self.assertEqual(pacer.throttled("site", retry_after=left), first)
        clock.now += 61  # the site has reopened: a refusal now is a new one
        self.assertGreater(pacer.throttled("site", retry_after=10), first)

    def test_a_wait_that_is_not_a_number_of_seconds_holds_nothing(self) -> None:
        # "Retry-After: inf" held the site for the life of the process.
        for nonsense in (float("inf"), float("nan"), -5.0):
            with self.subTest(retry_after=nonsense):
                pacer = SitePacer(pacing_for(0))
                pacer.throttled("site", retry_after=nonsense)
                stop = threading.Event()
                waiter = threading.Thread(target=pacer.wait, args=("site", stop), daemon=True)
                waiter.start()
                waiter.join(1.0)
                held = waiter.is_alive()
                stop.set()  # let a held one go, so a failure does not hang the run
                self.assertFalse(held)

    def test_refused_again_right_after_a_block_doubles_the_gap(self) -> None:
        # The estimate divides the site's wait by what got through, which right
        # after a block is only the few sent since: 3 in 10s read as a 3.8s gap.
        clock = Clock()
        pacer = SitePacer(pacing_for(0), clock=clock)
        for _ in range(10):
            clock.now += 0.2
            pacer.eased("site")
        first = pacer.throttled("site", retry_after=10)
        clock.now += 10
        for _ in range(3):
            clock.now += first
            pacer.eased("site")
        before = pacer.interval_for("site")
        self.assertAlmostEqual(pacer.throttled("site", retry_after=10), before * 2, places=3)

    def test_easing_never_goes_past_the_longest_gap(self) -> None:
        clock = Clock()
        pacer = SitePacer(PacingPolicy(interval=0.0, floor=0.05, max_interval=120.0), clock=clock)
        pacer.learn("site", 1.0)
        for _ in range(12):
            pacer.throttled("site")  # refused again and again: up to the longest gap
            clock.now += 61
        for _ in range(10):  # ten successes, while the refusal is remembered
            clock.now += 1
            pacer.eased("site")
        self.assertLessEqual(pacer.interval_for("site"), 120.0)

    def test_a_turn_given_up_is_given_back(self) -> None:
        # A request cancelled while waiting kept its turn, so the next one
        # waited for it as well.
        pacer = SitePacer(PacingPolicy(interval=0.5, floor=0.5, ceiling=0.5, pause_chance=0))
        pacer.wait("site")  # the first goes at once
        cancel = threading.Event()
        threading.Timer(0.1, cancel.set).start()
        with self.assertRaises(Exception):
            pacer.wait("site", cancel)  # would go at +0.5, cancelled at +0.1
        started = time.monotonic()
        pacer.wait("site")  # takes the +0.5 turn back, not +1.0
        self.assertLess(time.monotonic() - started, 0.6)

    def test_nothing_is_sent_while_the_site_says_wait(self) -> None:
        pacer = SitePacer(pacing_for(0))
        pacer.throttled("site", retry_after=0.4)
        started = time.monotonic()
        pacer.wait("site")
        self.assertGreaterEqual(time.monotonic() - started, 0.35)
        started = time.monotonic()
        pacer.wait("other")  # another site is not held up by it
        self.assertLess(time.monotonic() - started, 0.3)

    def test_threads_take_turns_so_the_pace_holds_however_many_wait(self) -> None:
        # The library's pacer let each waiting thread time itself from the last
        # request sent, so two went out per gap: twice the pace, and a refusal.
        pacer = SitePacer(PacingPolicy(interval=0.03, floor=0.01))
        pacer.throttled("site", retry_after=0.01)  # a site that counts: paced evenly
        time.sleep(0.05)
        sent: List[float] = []
        lock = threading.Lock()

        def worker() -> None:
            for _ in range(5):
                pacer.wait("site")
                with lock:
                    sent.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(8)]
        started = time.monotonic()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        gap = pacer.interval_for("site")
        sent.sort()
        closest = min(b - a for a, b in zip(sent, sent[1:]))
        self.assertGreater(closest, gap * 0.6)
        self.assertGreater(time.monotonic() - started, 39 * gap * 0.85)


class _WindowedSite(BaseHTTPRequestHandler):
    """Allows LIMIT requests per WINDOW seconds; the rest get 429 + Retry-After."""

    LIMIT = 6
    WINDOW = 2.0
    lock = threading.Lock()
    window_start = 0.0
    used = 0
    served: List[float] = []
    refused = 0

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
            else:
                cls.refused += 1
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
        _WindowedSite.refused = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _WindowedSite)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.origin = f"http://127.0.0.1:{self.server.server_port}/"

    def scraper(self, origin: str) -> Scraper:
        config = ScraperConfig(
            pacing=pacing_for(0),
            data_dir=Path(self.directory.name),
            raise_for_status=False,
        )
        scraper = Scraper(
            origin=origin, config=config, state=site_state(config, Memory(config.memory_path))
        )
        self.addCleanup(scraper.close)
        return scraper

    def test_a_windowed_limit_is_found_and_followed(self) -> None:
        scraper = self.scraper(self.origin)
        for page in range(40):
            response = scraper.fetch("GET", f"{self.origin}chapter-{page}")
            self.assertEqual(response.status_code, 200)
        # The site takes 6 every 2 seconds. One refusal says so; the library
        # took its Retry-After as the gap (one request every 1-2s), and doubling
        # up from nothing was refused again and again on the way.
        interval = scraper.pacer.interval_for(scraper.memory.key(self.origin))
        self.assertLess(interval, 0.8)
        self.assertLessEqual(_WindowedSite.refused, 3)


class _SlowSite(BaseHTTPRequestHandler):
    """Takes a moment to answer, and notes how many requests it had at once."""

    lock = threading.Condition()
    now = {"image": 0, "page": 0}
    most = {"image": 0, "page": 0}
    # Images are held until this many are being answered at once (or a couple
    # of seconds pass). Answered after a fixed moment, a busy machine opened its
    # eighth connection only after the first had been answered, and counted 7.
    hold_images_for = 0

    def do_GET(self) -> None:
        cls = type(self)
        kind = "image" if self.path.startswith("/img") else "page"
        with cls.lock:
            cls.now[kind] += 1
            cls.most[kind] = max(cls.most[kind], cls.now[kind])
            cls.lock.notify_all()
            if kind == "image":
                cls.lock.wait_for(lambda: cls.most["image"] >= cls.hold_images_for, timeout=2)
        time.sleep(0.15)  # long enough for one connection too many to be seen
        with cls.lock:
            cls.now[kind] -= 1
        body = b"<html><body><p>" + b"text " * 50 + b"</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


class ImageConnectionTests(unittest.TestCase):
    def test_page_images_load_over_more_connections_than_pages(self) -> None:
        # A manga's page images come from an image host that answered 12 a
        # second over two connections, 28 over four and 40 over eight, with
        # nothing refused. The library's two connections per site are right for
        # a site's pages; images load over a few more, as a browser loads them.
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
        _SlowSite.now = {"image": 0, "page": 0}
        _SlowSite.most = {"image": 0, "page": 0}
        _SlowSite.hold_images_for = scraping.IMAGE_CONNECTIONS
        server = ThreadingHTTPServer(("127.0.0.1", 0), _SlowSite)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        origin = f"http://127.0.0.1:{server.server_port}/"
        config = ScraperConfig(
            pacing=pacing_for(0), data_dir=Path(directory.name), raise_for_status=False
        )
        scraper = Scraper(
            origin=origin, config=config, state=site_state(config, Memory(config.memory_path))
        )
        scraping._install_pacing(scraper)
        self.addCleanup(scraper.close)
        scraper.fetch("GET", f"{origin}page/first")  # the visit to the site itself

        def fetch(kind: str, number: int) -> None:
            if kind == "image":
                with scraping.fetching_image():
                    scraper.fetch("GET", f"{origin}img/{number}", navigation=False)
            else:
                scraper.fetch("GET", f"{origin}page/{number}")

        for kind in ("image", "page"):
            threads = [threading.Thread(target=fetch, args=(kind, number)) for number in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(_SlowSite.most["image"], scraping.IMAGE_CONNECTIONS)
        # eight, by measurement: twice the images a second of four, none refused
        self.assertEqual(_SlowSite.most["image"], 8)
        self.assertLessEqual(_SlowSite.most["page"], 2)


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

    def test_a_task_can_be_sent_without_waiting_for_the_rate(self) -> None:
        from lncrawl.core.taskman import TaskManager

        taskman = TaskManager(workers=4, ratelimit=1.0)  # one a second
        self.addCleanup(taskman.close)
        with request_budget(threading.Event(), 5) as budget:
            started = time.monotonic()
            futures = [taskman.submit_unpaced(current_budget) for _ in range(4)]
            seen = [future.result(timeout=5) for future in futures]
        self.assertLess(time.monotonic() - started, 1.0)  # not three seconds of turns
        self.assertTrue(all(task_budget is budget for task_budget in seen))

    def test_waiting_at_the_scrapers_connection_gate_does_not_use_up_the_limit(self) -> None:
        fake = SimpleNamespace(_paced=lambda key, abort: _slow_gate(0.2))
        scraping._install_pacing(fake)
        with request_budget(threading.Event(), 0.1) as budget:
            with fake._paced("site", None):
                budget.remaining()


if __name__ == "__main__":
    unittest.main()
