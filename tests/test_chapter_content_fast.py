"""has_chapter_content's lxml walk agrees with the BeautifulSoup original."""

import unittest
import warnings

from lncrawl.utils import chapter_content
from lncrawl.utils.chapter_content import NOVELFIRE_LOGIN_NOTICE, has_chapter_content

NOTICE = NOVELFIRE_LOGIN_NOTICE

BODIES = [
    "<p>Real story text.</p>",
    "<p></p><div> </div>",
    "<p>​﻿</p>",
    "<script>var story = 'not shown';</script>",
    "<style>p { color: red }</style><template><p>hidden</p></template>",
    "<template><img src='a.jpg'></template>",
    "<p><img src='page-1.jpg'></p>",
    "<p><img src='  '></p>",
    "<p><img></p>",
    "<!-- only a comment -->",
    "<p>a<!-- c -->b</p>",
    f"<p>{NOTICE}</p>",
    f"<h2>Chapter 4: A title</h2><p>{NOTICE}</p>",
    f"<h2>Chapter 4</h2><p>{NOTICE}</p><p>And then the story.</p>",
    f"<h2>Chapter 4 <img src='x.jpg'></h2><p>{NOTICE}</p>",
    f"<h2>Chapter 4</h2><p>{NOTICE}</p><p><img src='x.jpg'></p>",
    f"<h2>Chapter</h2>after heading<p>{NOTICE}</p>",
    f"<p>{NOTICE.upper()}</p>",
    "<div><h1>Only a heading</h1></div>",
    "plain text, no tags",
    "<html><body><p>Unclosed <i>tags <b>everywhere</p>more",
    "<?xml version='1.0'?><p>xml-ish</p>",
    "<p>café 中文</p>",
]


class ChapterContentFastTests(unittest.TestCase):
    def test_agrees_with_the_beautifulsoup_version(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # bs4 warns about the XML-looking body
            for body in BODIES:
                with self.subTest(body=body[:50]):
                    self.assertEqual(
                        chapter_content._has_content(body),
                        chapter_content._has_content_slow(body),
                    )

    def test_empty_input_and_repeats(self) -> None:
        self.assertFalse(has_chapter_content(""))
        self.assertFalse(has_chapter_content("   "))
        chapter_content._has_content.cache_clear()
        body = "<p>Real story text.</p>"
        self.assertTrue(has_chapter_content(body))
        self.assertTrue(has_chapter_content(str(body)))
        self.assertEqual(chapter_content._has_content.cache_info().hits, 1)


if __name__ == "__main__":
    unittest.main()
