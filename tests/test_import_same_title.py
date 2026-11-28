import unittest
from unittest.mock import patch

import test_novel_repair

from lncrawl.dao import Novel
from lncrawl.services import importer


class SameTitleImportTests(unittest.TestCase):
    """A manga file named like a library novel joins it only when it is that novel's own book."""

    def setUp(self) -> None:
        fixture = test_novel_repair.NovelRepairTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.ctx = fixture.ctx
        fixture.stack.enter_context(patch.object(importer, "ctx", self.ctx))

    def test_a_book_of_the_novels_own_chapters_is_that_novel(self) -> None:
        found = importer._named_like("Test Novel.cbz", {1, 2, 3})  # the novel lists 1-4
        assert found is not None
        self.assertEqual(found.id, "novel")

    def test_the_same_title_from_elsewhere_stays_its_own_copy(self) -> None:
        # chapters the novel does not list: another site's copy, not this novel's book
        self.assertIsNone(importer._named_like("Test Novel.cbz", set(range(1, 60))))
        with self.ctx.db.session() as sess:
            self.assertIsNotNone(sess.get(Novel, "novel"))


if __name__ == "__main__":
    unittest.main()
