"""The desktop app lives as long as its window: a window that crashed is opened again."""

from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from lncrawl.server import webview

# What Windows reports for a process that crashed (an access violation).
CRASHED = 3221225477


class ReopenTests(unittest.TestCase):
    def test_a_window_closed_by_the_person_is_not_opened_again(self) -> None:
        self.assertFalse(webview._reopen(0, [], 0.0))
        self.assertFalse(webview._reopen(None, [], 0.0))

    def test_a_window_that_crashed_is_opened_again_a_few_times(self) -> None:
        reopened: list = []
        answers = [webview._reopen(CRASHED, reopened, float(n)) for n in range(7)]
        self.assertEqual(answers, [True] * webview.REOPEN_LIMIT + [False, False])

    def test_crashes_long_apart_are_counted_afresh(self) -> None:
        reopened: list = []
        for n in range(webview.REOPEN_LIMIT):
            self.assertTrue(webview._reopen(CRASHED, reopened, float(n)))
        later = webview.REOPEN_WINDOW + 100.0
        self.assertTrue(webview._reopen(CRASHED, reopened, later))


class StartTests(unittest.TestCase):
    def run_app(self, codes: list) -> MagicMock:
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        ctx = SimpleNamespace(
            config=SimpleNamespace(
                load=MagicMock(), app=SimpleNamespace(app_dir=Path(temp.name), build="dev")
            )
        )
        window = MagicMock(side_effect=codes)
        with (
            patch.object(webview, "ctx", ctx),
            patch.object(webview, "free_port", return_value=1),
            patch.object(webview, "log_to_file", return_value=None),
            patch.object(webview, "enable_crash_log"),
            patch.object(webview, "Thread"),
            patch.object(webview, "_start_app_in_browser", window),
        ):
            webview.start()
        return window

    def test_the_app_ends_when_its_window_is_closed(self) -> None:
        self.assertEqual(self.run_app([0]).call_count, 1)

    def test_a_window_that_crashed_is_opened_again_and_the_app_goes_on(self) -> None:
        self.assertEqual(self.run_app([CRASHED, CRASHED, 0]).call_count, 3)


CRASH = """
import sys
from pathlib import Path
from lncrawl.services.logger import enable_crash_log
enable_crash_log(Path(sys.argv[1]))
import threading
def fails():
    raise ValueError("something nobody caught")
t = threading.Thread(target=fails, name="downloader-1")
t.start(); t.join()
import ctypes
ctypes.string_at(0)  # a crash in native code
"""


class CrashLogTests(unittest.TestCase):
    def test_a_crash_leaves_every_threads_stack_and_uncaught_errors_are_logged(self) -> None:
        with TemporaryDirectory() as temp:
            log = Path(temp) / "logs" / "crash.log"
            result = subprocess.run(
                [sys.executable, "-c", CRASH, str(log)],
                cwd=Path(__file__).resolve().parents[1],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Uncaught error in downloader-1", result.stderr)
            self.assertIn("something nobody caught", result.stderr)
            written = log.read_text()
            self.assertIn("--- app started", written)
            self.assertIn("Fatal Python error", written)
            self.assertIn("Current thread", written)


if __name__ == "__main__":
    unittest.main()
