"""The Reader's per-novel summaries answer the same as one grouped query would."""

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine

from lncrawl.dao import Chapter, ChapterImage, Novel
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


class HealFromLibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        import test_novel_repair

        fixture = test_novel_repair.NovelRepairTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.ctx = fixture.ctx
        self.engine = fixture.engine

    def test_only_saved_text_chapters_of_another_copy_fill_the_gaps(self) -> None:
        # the fixture's novel: chapter 1 saved, 2 saved empty, 3 and 4 missing
        with Session(self.engine) as session:
            session.add(
                Novel(
                    id="copy",
                    title="Test Novel",
                    url="https://example.com/copy",
                    domain="example.com",
                    chapter_count=4,
                )
            )
            for serial in range(1, 5):
                session.add(
                    Chapter(
                        id=f"copy-{serial}",
                        novel_id="copy",
                        serial=serial,
                        title=f"Chapter {serial}",
                        url=f"https://example.com/copy/{serial}",
                        is_done=True,
                        extra={},
                    )
                )
            session.add(
                ChapterImage(id="page", novel_id="copy", chapter_id="copy-4", url="https://x/p.png")
            )
            session.commit()
        for serial in (2, 3, 4):  # 1 is not saved in the copy
            self.ctx.files.save_text(
                f"novels/copy/chapters/{serial:06}.zst", f"<p>Copied text {serial}.</p>"
            )

        result = self.ctx.novels.heal_from_library("novel")

        self.assertEqual(result["healed"], 2)  # 2 (was empty) and 3; 4 is a manga page
        self.assertEqual(
            self.ctx.files.load_text("novels/novel/chapters/000003.zst"), "<p>Copied text 3.</p>"
        )
        self.assertFalse(self.ctx.files.exists("novels/novel/chapters/000004.zst"))
        self.assertEqual(
            self.ctx.files.load_text("novels/novel/chapters/000001.zst"),
            "<p>Existing chapter one.</p>",
        )


if __name__ == "__main__":
    unittest.main()
