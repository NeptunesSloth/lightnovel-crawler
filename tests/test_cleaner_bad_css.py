"""The cleaner's own matching of simple bad_css selectors equals soupsieve's."""

import unittest

from bs4 import BeautifulSoup

from lncrawl.core.cleaner import TextCleaner

SELECTORS = {
    # simple: tag, class, id, tag with class/id
    "script",
    "iframe",
    "DIV",
    ".ads",
    ".sharedaddy",
    "#comments",
    "p.notice",
    "span#tip",
    "div.code-block",
    ".Ads-Mixed",
    # not simple: soupsieve decides these
    "a[href*='patreon.com']",
    "div[style*='display:none']",
    "p > strong > strong",
    "div.Readpage.pagedown",
    ".epwrapper > div:not(.epcontent)",
    "p:nth-child(1) > strong",
    "span[id^='ad-']",
    "div[class*='banner']",
    "img[src*='pixel']",
}

PAGES = [
    """<div id="c"><p>Story <span class="ads x">ad</span></p>
       <div class="code-block">x</div><div class="code-block-2">kept</div>
       <p class="notice">n</p><span class="notice">kept</span><span id="tip">t</span>
       <section id="comments"><p>c</p></section><iframe src="x"></iframe>
       <a href="https://patreon.com/me">p</a><a href="/next">kept</a>
       <div style="display:none">h</div><p><strong><strong>s</strong></strong></p>
       <div class="Readpage pagedown">r</div><div class="Readpage">kept</div>
       <div class="epwrapper"><div class="epcontent">kept</div><div>gone</div></div>
       <span id="ad-1">a</span><div class="top-banner">b</div><img src="/pixel.gif">
       <div class="ads-mixed">kept: classes are case-sensitive</div>
       <div class="Ads-Mixed">gone</div><DIV>upper-case tag</DIV>
       <p>First <strong>p</strong></p></div>""",
    "<div><p>nothing to remove</p><p class=''>empty class</p><p id=''>empty id</p></div>",
    "<div><div><div class='ads'><p class='ads'>nested</p></div></div></div>",
]


class BadCssTests(unittest.TestCase):
    def test_selects_what_soupsieve_selects(self) -> None:
        cleaner = TextCleaner()
        cleaner.bad_css = set(SELECTORS)
        for page in PAGES:
            with self.subTest(page=page[:40]):
                div = BeautifulSoup(page, "lxml").find("div")
                expected = {id(tag) for tag in div.select(",".join(cleaner.bad_css))}
                got = [id(tag) for tag in cleaner._bad_css_matches(div)]
                self.assertEqual(len(got), len(set(got)), "an element twice")
                self.assertEqual(set(got), expected)

    def test_the_default_selectors_and_a_source_adding_more(self) -> None:
        cleaner = TextCleaner()
        div = BeautifulSoup(PAGES[0], "lxml").find("div")
        expected = {id(tag) for tag in div.select(",".join(cleaner.bad_css))}
        self.assertEqual({id(tag) for tag in cleaner._bad_css_matches(div)}, expected)
        cleaner.bad_css.update([".notice", "a[href*='patreon.com']"])  # as sources do
        expected = {id(tag) for tag in div.select(",".join(cleaner.bad_css))}
        self.assertEqual({id(tag) for tag in cleaner._bad_css_matches(div)}, expected)


if __name__ == "__main__":
    unittest.main()
