"""The Reader's per-novel summaries answer the same as one grouped query would."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine

from lncrawl.dao import Chapter, Novel
from lncrawl.server.api import novels as novels_api


class LatestChaptersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)
        ctx = SimpleNamespace(db=SimpleNamespace(session=lambda: Session(self.engine)))
        patcher = patch.object(novels_api, "ctx", ctx)
        patcher.start()
        self.addCleanup(patcher.stop)

    def add(self, novel_id: str, chapters: list) -> None:
        with Session(self.engine) as session:
            session.add(
                Novel(
                    id=novel_id,
                    title=novel_id,
                    url=f"https://example.com/{novel_id}",
                    domain="example.com",
                )
            )
            for serial, (created_at, done) in enumerate(chapters, start=1):
                session.add(
                    Chapter(
                        id=f"{novel_id}-{serial}",
                        novel_id=novel_id,
                        serial=serial,
                        title=f"Chapter {serial}",
                        url=f"https://example.com/{novel_id}/{serial}",
                        is_done=done,
                        created_at=created_at,
                        extra={},
                    )
                )
            session.commit()

    def test_the_newest_chapter_counts_whether_downloaded_or_not(self) -> None:
        self.add("waiting", [(100, True), (300, False), (200, True)])
        self.add("read", [(100, False), (500, True)])
        self.add("all-done", [(50, True), (70, True)])
        self.add("none-done", [(40, False)])
        self.assertEqual(
            novels_api.latest_chapters(),
            {"waiting": 300, "read": 500, "all-done": 70, "none-done": 40},
        )

    def test_a_novel_without_chapters_is_left_out(self) -> None:
        self.add("details-only", [])
        self.add("one", [(10, True)])
        self.assertEqual(novels_api.latest_chapters(), {"one": 10})


if __name__ == "__main__":
    unittest.main()
