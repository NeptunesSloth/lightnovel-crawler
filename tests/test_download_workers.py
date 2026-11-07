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
    def run_app(self, workers: int, mode: str) -> dict:
        with TemporaryDirectory() as data:
            result = subprocess.run(
                [sys.executable, str(APP), data, str(workers), "30", mode],
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

    def test_a_worker_that_dies_is_finished_in_process(self) -> None:
        outcome = self.run_app(workers=2, mode="kill")
        self.assertTrue(outcome["killed"])
        self.assertEqual(outcome["statuses"], ["SUCCESS", "SUCCESS"])
        self.assertEqual(outcome["available"], 60)


if __name__ == "__main__":
    unittest.main()
