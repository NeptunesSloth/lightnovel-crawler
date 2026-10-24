import asyncio
from concurrent.futures import TimeoutError
import importlib.util
from pathlib import Path
from threading import BoundedSemaphore, Event
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from lncrawl.core.request_budget import CrawlTimeoutError, request_budget
from lncrawl.exceptions import LNException
from lncrawl.utils.async_loop import run_async
from lncrawl.webdriver import job_queue


def _load_browser_factory():
    # These tests exercise our reservation/cleanup logic with a mocked browser
    # backend. Load it separately so optional nodriver runtime compatibility
    # does not affect the resource tests or replace the application's module.
    spec = importlib.util.spec_from_file_location(
        "lncrawl.webdriver._resource_test_local", Path(job_queue.__file__).with_name("local.py")
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        "sys.modules", {"nodriver": SimpleNamespace(start=MagicMock(), Browser=object)}
    ):
        spec.loader.exec_module(module)
    return module


local = _load_browser_factory()


class BrowserSlotTests(unittest.TestCase):
    def test_wait_for_busy_browser_pool_obeys_chapter_budget(self) -> None:
        semaphore = BoundedSemaphore(0)
        with patch.object(job_queue, "__semaphore", semaphore), request_budget(Event(), 0.01):
            with self.assertRaises(CrawlTimeoutError):
                job_queue.acquire_queue(timeout=120)

    def test_missing_executable_does_not_consume_browser_slot(self) -> None:
        semaphore = BoundedSemaphore(1)
        with (
            patch.object(job_queue, "__semaphore", semaphore),
            patch.object(local, "pick_executable", return_value=None),
        ):
            for _ in range(12):
                with self.assertRaises(LNException):
                    local.create_local(headless=True, timeout=0.01)
            self.assertTrue(semaphore.acquire(blocking=False))

    def test_repeated_launch_failures_return_reserved_slot(self) -> None:
        semaphore = BoundedSemaphore(1)
        with (
            patch.object(job_queue, "__semaphore", semaphore),
            patch.object(local, "pick_executable", return_value="/test/chrome"),
            patch.object(local.nodriver, "start", return_value=object()),
            patch.object(
                local, "run_async", side_effect=TimeoutError("Browser startup timed out")
            ) as launch,
        ):
            for _ in range(12):
                with self.assertRaisesRegex(TimeoutError, "Browser startup timed out"):
                    local.create_local(headless=True, timeout=0.01)
            self.assertEqual(launch.call_count, 12)
            self.assertTrue(semaphore.acquire(blocking=False))

    def test_successful_browser_holds_slot_until_stopped(self) -> None:
        semaphore = BoundedSemaphore(1)
        browser = SimpleNamespace(stop=MagicMock())
        with (
            patch.object(job_queue, "__semaphore", semaphore),
            patch.object(job_queue, "__open_browsers", []),
            patch.object(local, "pick_executable", return_value="/test/chrome"),
            patch.object(local.nodriver, "start", return_value=object()),
            patch.object(local, "run_async", return_value=browser),
        ):
            self.assertIs(local.create_local(headless=True, timeout=0.01), browser)
            self.assertFalse(semaphore.acquire(blocking=False))
            browser.stop()
            browser.stop()
            self.assertTrue(semaphore.acquire(blocking=False))


class AsyncCancellationTests(unittest.TestCase):
    def test_timed_out_coroutine_is_cancelled(self) -> None:
        started = Event()
        cancelled = Event()

        async def operation():
            started.set()
            try:
                await asyncio.sleep(3600)
            finally:
                cancelled.set()

        with self.assertRaises(TimeoutError):
            run_async(operation(), timeout=0.05)
        self.assertTrue(started.is_set())
        self.assertTrue(cancelled.wait(1), "Timed-out operation was left running")

    def test_successful_async_result_is_preserved(self) -> None:
        async def operation():
            return 42

        self.assertEqual(run_async(operation(), timeout=1), 42)


if __name__ == "__main__":
    unittest.main()
