"""lncrawl's adjustments to the scraper package's page checks."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scraper import session as scraper_session

from lncrawl.core import scraping


def response(content_type: str) -> SimpleNamespace:
    return SimpleNamespace(headers={"content-type": content_type})


class DecoyCheckTests(unittest.TestCase):
    def inspect(self, content_type: str) -> bool:
        with patch.object(scraping, "_inspect_content") as original:
            scraper_session.Scraper._inspect_content(
                object(), "https://api.example.com/x", "key", object(), response(content_type)
            )
        return original.called

    def test_an_api_answer_is_not_checked_as_a_page(self) -> None:
        self.assertFalse(self.inspect("application/json"))
        self.assertFalse(self.inspect("application/vnd.api+json; charset=utf-8"))

    def test_pages_are_still_checked(self) -> None:
        self.assertTrue(self.inspect("text/html; charset=utf-8"))
        self.assertTrue(self.inspect(""))


if __name__ == "__main__":
    unittest.main()
