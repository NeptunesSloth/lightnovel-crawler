from types import SimpleNamespace
from typing import List
import unittest
from unittest.mock import patch

from lncrawl.services.scheduler.handlers import _base


class _Handler(_base.BaseHandler):
    @staticmethod
    def can_activate(job: object) -> bool:
        return True

    def run(self) -> None:
        pass


class JobSettleTests(unittest.TestCase):
    """How a job ended is written again when the database was busy the first time."""

    def test_a_busy_database_does_not_leave_the_job_running(self) -> None:
        tries: List[int] = []

        def success() -> bool:
            tries.append(1)
            if len(tries) < 3:
                raise RuntimeError("database is locked")
            return True

        fake = SimpleNamespace(logger=SimpleNamespace(info=print, debug=print, warn=print))
        job = SimpleNamespace(id="j", status=SimpleNamespace(name="PENDING"), job_title="t")
        with patch.object(_base, "ctx", fake), patch.object(_base, "SETTLE_WAIT", 0.01):
            handler = _Handler(job)  # type: ignore[arg-type]
            handler.job.parent_job_id = "p"
            with patch.object(handler, "_set_success", success):
                self.assertTrue(handler.process())
        self.assertEqual(len(tries), 3)


if __name__ == "__main__":
    unittest.main()
