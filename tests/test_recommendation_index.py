"""The recommendations title index takes in novels made after it was built."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine

from lncrawl.dao import Novel
from lncrawl.services import recommendations


class CatchUpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)
        ctx = SimpleNamespace(db=SimpleNamespace(session=lambda: Session(self.engine)))
        patcher = patch.object(recommendations, "ctx", ctx)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.index = recommendations._InvertedIndex()

    def add(self, novel_id: str, title: str, created_at: int) -> None:
        with Session(self.engine) as session:
            session.add(
                Novel(
                    id=novel_id,
                    title=title,
                    url=f"https://example.com/{novel_id}",
                    domain="example.com",
                    created_at=created_at,
                )
            )
            session.commit()

    def found(self, *words: str) -> list:
        return sorted(self.index.candidates(set(words), exclude_id="", limit=10))

    def test_a_novel_made_by_another_process_is_found_after_the_build(self) -> None:
        self.add("old", "Sword Saint", 100)
        self.index.build()
        # a download worker process made this one: nothing here was told
        self.add("new", "Sword Emperor", 200)
        self.add("same-time", "Sword God", 100)
        self.assertEqual(self.found("sword"), ["old"])
        self.index.catch_up()
        self.assertEqual(self.found("sword"), ["new", "old", "same-time"])
        self.index.catch_up()  # nothing counted twice
        self.assertEqual(self.index._total, 3)

    def test_a_removed_novel_goes_once(self) -> None:
        self.add("old", "Sword Saint", 100)
        self.index.build()
        self.index.remove("old", "Sword Saint")
        self.index.remove("old", "Sword Saint")
        self.assertEqual((self.found("sword"), self.index._total), ([], 0))


if __name__ == "__main__":
    unittest.main()
