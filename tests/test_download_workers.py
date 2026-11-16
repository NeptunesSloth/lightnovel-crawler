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


# A worker that loads the app and says it is ready, then never begins its job.
STUCK_WORKER = """
import sys
import time

if "--multiprocessing-fork" in sys.argv:
    import lncrawl.services.scheduler.workers as workers

    workers._run_jobs = lambda *args, **kwargs: time.sleep(3600)
"""
# The packaged app: every process is the one frozen executable, whose entry is
# lncrawl/__main__.py, and a worker is that executable started again.
PACKAGED_APP = """
import sys

sys.frozen = True
import multiprocessing

multiprocessing.set_executable({exe!r})
"""
PACKAGED_EXE = """#!{python}
import runpy
import sys

sys.frozen = True
runpy.run_path({entry!r}, run_name="__main__")
"""


class DownloadWorkerTests(unittest.TestCase):
    def run_app(
        self,
        workers: int,
        mode: str,
        chapters: int = 30,
        boot_delay: int = 0,
        hook: str = "",
        packaged: bool = False,
        wait: int = 120,
    ) -> dict:
        with TemporaryDirectory() as data, TemporaryDirectory() as hooks:
            path = str(ROOT)
            if boot_delay:
                # hold up ctx.setup in the worker processes only, as a slow or
                # stuck start of the packaged Windows app would
                hook = SLOW_BOOT.format(delay=boot_delay)
            if packaged:
                exe = Path(hooks, "lncrawl-packaged")
                exe.write_text(
                    PACKAGED_EXE.format(
                        python=sys.executable, entry=str(ROOT / "lncrawl" / "__main__.py")
                    )
                )
                exe.chmod(0o755)
                hook = PACKAGED_APP.format(exe=str(exe)) + hook
            if hook:
                Path(hooks, "sitecustomize.py").write_text(hook)
                path = os.pathsep.join([hooks, path])
            result = subprocess.run(
                [sys.executable, str(APP), data, str(workers), str(chapters), mode],
                cwd=ROOT,
                env=dict(os.environ, PYTHONPATH=path, LNCRAWL_TEST_WAIT=str(wait)),
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

    @unittest.skipUnless(
        sys.version_info >= (3, 13) and sys.platform != "win32",
        "starts a frozen app's worker the way Python 3.13+ does off Windows",
    )
    def test_exports_run_in_the_workers_of_the_packaged_app(self) -> None:
        # In the packaged app a worker ran its whole life from inside the app's
        # own `import lncrawl` (freeze_support was called there). Python 3.13+
        # makes other threads wait for a package that is still being imported,
        # so the thread given the job waited for ever on its first import: the
        # export sat at "Starting" and never downloaded anything.
        outcome = self.run_app(workers=2, mode="export", packaged=True, wait=100)
        self.assertEqual(outcome["statuses"], ["SUCCESS", "SUCCESS"])
        self.assertEqual(outcome["available"], 60)
        self.assertEqual(outcome["workers_seen"], 2)
        # done by the workers themselves, not by the app after giving up on them
        self.assertLess(outcome["seconds"], 40)

    def test_an_export_its_worker_never_begins_runs_in_the_app_instead(self) -> None:
        # A worker can load the app, say it is ready, and still never begin
        # the job. Nothing noticed, and the export waited on it for good.
        outcome = self.run_app(workers=2, mode="export-stuck-worker", hook=STUCK_WORKER, wait=60)
        self.assertEqual(outcome["statuses"], ["SUCCESS", "SUCCESS"])
        self.assertEqual(outcome["available"], 60)

    def test_a_worker_that_dies_is_finished_in_process(self) -> None:
        outcome = self.run_app(workers=2, mode="kill")
        self.assertTrue(outcome["killed"])
        self.assertEqual(outcome["statuses"], ["SUCCESS", "SUCCESS"])
        self.assertEqual(outcome["available"], 60)


class BeginTimeoutTests(unittest.TestCase):
    def run_on(self, active: int) -> tuple:
        from threading import Event, Timer
        from types import SimpleNamespace
        from unittest.mock import MagicMock, patch

        from lncrawl.services.scheduler import workers

        done = Event()
        Timer(1.2, done.set).start()  # the worker gets to it in the end
        worker = SimpleNamespace(
            ready=Event(),
            began={1: Event()},  # never says it began this one
            started_at=0.0,
            active=active,
            submit=lambda request_id, job_ids, concurrency: (done, [None]),
            cancel=MagicMock(),
            abandon=MagicMock(),
        )
        worker.ready.set()
        pool = workers.DownloadWorkers()
        pool._ids = iter([1])
        with patch.object(workers, "BEGIN_SECONDS", 0.3):
            finished = pool._run_on(worker, "site", ["job"], 1, Event())
        return finished, worker.abandon.called

    def test_a_worker_that_never_begins_its_only_work_is_given_up(self) -> None:
        finished, abandoned = self.run_on(active=1)
        self.assertTrue(abandoned)
        self.assertFalse(finished)

    def test_a_worker_busy_with_other_work_is_not_given_up(self) -> None:
        # It already runs this site's export; giving up on it for a batch that
        # waits its turn there would kill that export as well.
        finished, abandoned = self.run_on(active=2)
        self.assertFalse(abandoned)
        self.assertTrue(finished)


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
