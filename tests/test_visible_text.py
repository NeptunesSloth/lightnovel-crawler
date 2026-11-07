"""The scraper's page-text extraction is replaced with a faster one (core/scraping.py)."""

import unittest
import warnings

from scraper import session as scraper_session

from lncrawl.core import scraping

PARA = "<p>" + "The quick brown fox jumps over the lazy dog. " * 8 + "</p>"
JUNK = '<div class="ad"><script>var x = 1;</script><span>noise</span><!-- c --></div>' * 5

PAGES = [
    f"<html><head><title>T</title><style>p{{}}</style></head><body>{JUNK}"
    f"<div id='content'><h2>Chapter 1</h2>{PARA * 5}</div>{JUNK}"
    "<noscript>enable js</noscript></body></html>",
    "<div>a<!-- hidden -->b</div>",
    "<div>a<!--x--><!--y-->b<!--z--></div>c",
    "<div>a<script>x</script>b</div>tail",
    "<p>Hello <b>world</b> &amp; friends</p>",
    "<?xml version='1.0'?><p>pi</p>after",
    "<div><style>.a{}</style>visible<script>bad()</script>end</div>",
    "<template><p>t</p></template>after",
    "<html><body><p>Unclosed <i>tags <b>everywhere</p>more",
    "plain text with no tags",
    "",
    "   ",
    "<p>x<br>y</p>",
    "<table><tr><td>1</td><td>2</td></tr></table>",
    "<p>café 中文 مرحبا</p>",
    "<p>&nbsp;spaced&nbsp;</p>",
    "<html><head><title>Just a moment...</title></head>"
    "<body><div>Checking your browser</div></body></html>",
    "<!DOCTYPE html><html><body><!-- top -->Text<svg><text>svg text</text></svg></body></html>",
]


class VisibleTextTests(unittest.TestCase):
    def test_scraper_uses_the_fast_extraction(self) -> None:
        self.assertIs(scraper_session._visible_text, scraping._visible_text)

    def test_the_same_page_twice_is_read_once(self) -> None:
        page = PAGES[0]
        first = scraping._visible_text(page, "lxml")
        original = scraping._extract_visible_text
        try:
            scraping._extract_visible_text = lambda *a: self.fail("read again")
            self.assertEqual(scraping._visible_text(page, "lxml"), first)
        finally:
            scraping._extract_visible_text = original
        # a different page (even with equal text) is read afresh
        self.assertEqual(scraping._visible_text("<p>other</p>", "lxml"), "other")

    def test_same_words_as_the_original(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # bs4 warns about the XML-looking sample
            for page in PAGES:
                with self.subTest(page=page[:40]):
                    self.assertEqual(
                        scraping._visible_text(page, "lxml").split(),
                        scraping._slow_visible_text(page, "lxml").split(),
                    )


if __name__ == "__main__":
    unittest.main()
