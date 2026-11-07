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


class DownloadWorkerTests(unittest.TestCase):
    def run_app(self, workers: int, mode: str, chapters: int = 30) -> dict:
        with TemporaryDirectory() as data:
            result = subprocess.run(
                [sys.executable, str(APP), data, str(workers), str(chapters), mode],
                cwd=ROOT,
                env=dict(os.environ, PYTHONPATH=str(ROOT)),
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

    def test_a_worker_that_dies_is_finished_in_process(self) -> None:
        outcome = self.run_app(workers=2, mode="kill")
        self.assertTrue(outcome["killed"])
        self.assertEqual(outcome["statuses"], ["SUCCESS", "SUCCESS"])
        self.assertEqual(outcome["available"], 60)


if __name__ == "__main__":
    unittest.main()
