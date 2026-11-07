"""A chapter saved just before the process stopped is not left counted as missing."""

import unittest

import test_novel_repair

from lncrawl.dao import Chapter


class InterruptedSaveTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = test_novel_repair.NovelRepairTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.ctx = fixture.ctx

    def test_saved_text_with_an_unmarked_row_is_marked_without_downloading(self) -> None:
        # chapter 1's text is on disk; pretend the row update never happened
        with self.ctx.db.session() as sess:
            chapter = sess.get(Chapter, "chapter-1")
            chapter.is_done = False
            sess.add(chapter)
            sess.commit()

        self.ctx.crawler.fetch_chapter(self.fixture.user.id, "chapter-1")

        self.assertEqual(self.fixture.downloads, [])  # not fetched again
        with self.ctx.db.session() as sess:
            self.assertTrue(sess.get(Chapter, "chapter-1").is_done)


if __name__ == "__main__":
    unittest.main()
