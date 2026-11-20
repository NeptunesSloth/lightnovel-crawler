"""The time a novel's details may take, waiting for turns or not."""

import unittest
from unittest.mock import patch

from lncrawl.core import request_budget
from lncrawl.core.request_budget import CrawlTimeoutError, RequestBudget


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class RequestBudgetWallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        patcher = patch.object(request_budget, "monotonic", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_waiting_for_turns_stops_the_clock_but_not_the_wall(self) -> None:
        budget = RequestBudget(600, None, "Novel details request", wall=1800)
        with budget.paused():
            self.clock.now += 1700  # hours behind other novels' chapters, all but
        self.assertGreater(budget.remaining(), 0)
        self.assertLessEqual(budget.remaining(), 100)
        with budget.paused():
            self.clock.now += 200
        self.assertTrue(budget.is_set())
        with self.assertRaisesRegex(CrawlTimeoutError, "still going after 30 minutes"):
            budget.remaining()

    def test_without_a_wall_only_the_running_clock_counts(self) -> None:
        budget = RequestBudget(600, None)
        with budget.paused():
            self.clock.now += 100_000
        self.assertFalse(budget.is_set())
        self.assertEqual(budget.remaining(), 600)


if __name__ == "__main__":
    unittest.main()
