import unittest

from lncrawl.services.scheduler.handlers.export_source import _select_novels


class ExportLimitTests(unittest.TestCase):
    def test_duplicates_do_not_eat_into_the_requested_count(self) -> None:
        """Asking for N novels yields N, even when discovery repeats itself.

        A source turns the same novel up more than once — its search results and
        its sitemap overlap — so capping the raw list and de-duplicating the
        remainder returns fewer novels than were requested.
        """
        found = ["a", "a", "b", "c", "d"]

        self.assertEqual(_select_novels(found, 3), ["a", "b", "c"])

        # what the old order (cap, then de-duplicate) would have produced
        self.assertEqual(list(dict.fromkeys(found[:3])), ["a", "b"])

    def test_every_entry_duplicated_still_fills_the_limit(self) -> None:
        found = [url for name in "abcde" for url in (name, name)]
        self.assertEqual(_select_novels(found, 4), ["a", "b", "c", "d"])

    def test_limit_caps_a_larger_catalogue(self) -> None:
        self.assertEqual(_select_novels(["a", "b", "c", "d"], 2), ["a", "b"])

    def test_limit_beyond_the_catalogue_returns_what_exists(self) -> None:
        self.assertEqual(_select_novels(["a", "b"], 10), ["a", "b"])

    def test_discovery_order_is_preserved(self) -> None:
        """'The first N' is only meaningful if the order is the one found."""
        self.assertEqual(_select_novels(["d", "c", "b", "a"], 3), ["d", "c", "b"])

    def test_no_limit_keeps_everything_once(self) -> None:
        for limit in (None, 0):
            with self.subTest(limit=limit):
                self.assertEqual(_select_novels(["a", "b", "a", "c"], limit), ["a", "b", "c"])

    def test_blank_entries_are_dropped_rather_than_counted(self) -> None:
        """An empty URL must not take one of the requested slots."""
        self.assertEqual(_select_novels(["a", "", "b", "", "c"], 3), ["a", "b", "c"])

    def test_empty_discovery_stays_empty(self) -> None:
        self.assertEqual(_select_novels([], 5), [])


if __name__ == "__main__":
    unittest.main()
