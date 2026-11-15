"""Chapter downloads in real worker processes, through the real scheduler."""

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

APP = Path(__file__).with_name("download_workers_app.py")
ROOT = Path(__file__).resolve().parents[1]
SLOW_BOOT = """
import sys
import time

if "--multiprocessing-fork" in sys.argv:
    import lncrawl.context as context

    original = context.__AppContext__.setup

    def slow_setup(self, *args, **kwargs):
        time.sleep({delay})
        return original(self, *args, **kwargs)

    context.__AppContext__.setup = slow_setup
"""


class DownloadWorkerTests(unittest.TestCase):
    def run_app(self, workers: int, mode: str, chapters: int = 30, boot_delay: int = 0) -> dict:
        with TemporaryDirectory() as data, TemporaryDirectory() as hooks:
            path = str(ROOT)
            if boot_delay:
                # hold up ctx.setup in the worker processes only, as a slow or
                # stuck start of the packaged Windows app would
                Path(hooks, "sitecustomize.py").write_text(SLOW_BOOT.format(delay=boot_delay))
                path = os.pathsep.join([hooks, path])
            result = subprocess.run(
                [sys.executable, str(APP), data, str(workers), str(chapters), mode],
                cwd=ROOT,
                env=dict(os.environ, PYTHONPATH=path),
                capture_output=True,
                text=True,
                timeout=180,
            )
        output = (result.stdout + result.stderr)[-4000:]
        self.assertEqual(result.returncode, 0, output)
        lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
        self.assertTrue(lines, output)
        return json.loads(lines[-1])

    def test_two_sites_download_in_worker_processes(self) -> None:
        outcome = self.run_app(workers=2, mode="run")
        self.assertEqual(outcome["statuses"], ["SUCCESS", "SUCCESS"])
        self.assertEqual(outcome["available"], 60)
        self.assertEqual(outcome["workers_seen"], 2)  # one per site
        self.assertGreater(outcome["logs_from_workers"], 0)  # logged in the main process
        self.assertEqual(len(outcome["profiles_kept"]), 2)  # what they learned, kept

    def test_bulk_site_exports_run_in_worker_processes(self) -> None:
        outcome = self.run_app(workers=2, mode="export")
        self.assertEqual(outcome["statuses"], ["SUCCESS", "SUCCESS"])
        self.assertEqual(outcome["available"], 60)
        self.assertEqual(outcome["workers_seen"], 2)
        self.assertEqual(len(outcome["profiles_kept"]), 2)

    def test_cancelling_an_export_in_a_worker_stops_it(self) -> None:
        outcome = self.run_app(workers=2, mode="export-cancel", chapters=300)
        self.assertTrue(outcome["killed"])
        self.assertEqual(outcome["statuses"], ["CANCELED", "CANCELED"])
        self.assertLess(outcome["available"], 600)

    def test_an_export_whose_worker_does_not_start_runs_in_the_app_instead(self) -> None:
        # The export sat "Queued" with nothing to show for it while its worker
        # started, and would have forever had the start hung.
        outcome = self.run_app(workers=2, mode="export-slow-boot", boot_delay=30)
        self.assertEqual(outcome["statuses"], ["SUCCESS", "SUCCESS"])
        self.assertEqual(outcome["available"], 60)
        self.assertLess(outcome["seconds"], 25)  # not waiting out the 30s start
        self.assertIn("starting", outcome["phases"])  # said so meanwhile

    def test_a_worker_that_dies_is_finished_in_process(self) -> None:
        outcome = self.run_app(workers=2, mode="kill")
        self.assertTrue(outcome["killed"])
        self.assertEqual(outcome["statuses"], ["SUCCESS", "SUCCESS"])
        self.assertEqual(outcome["available"], 60)


class ConsoleWindowTests(unittest.TestCase):
    def test_a_worker_starts_without_a_console_window_of_its_own_on_windows(self) -> None:
        # The desktop app frees its console, so each worker opened an empty
        # black window, and closing it killed the downloads running in it.
        from types import SimpleNamespace
        from unittest.mock import patch

        from lncrawl.services.scheduler import workers

        calls = []
        fake = SimpleNamespace(CreateProcess=lambda *args: calls.append(args))
        start = ("lncrawl.exe", "cmd", None, None, False, 0, None, None, None)
        with patch.object(workers.sys, "platform", "win32"):
            with patch.dict(sys.modules, {"_winapi": fake}):
                with workers._without_console_window():
                    fake.CreateProcess(*start)  # as multiprocessing starts a worker
                fake.CreateProcess(*start)  # anything started afterwards
        self.assertEqual(calls[0][5], workers._CREATE_NO_WINDOW)
        self.assertEqual(calls[1][5], 0)


if __name__ == "__main__":
    unittest.main()
