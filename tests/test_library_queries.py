"""The Reader's per-novel summaries answer the same as one grouped query would."""

import importlib
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from sqlmodel import Session, SQLModel, create_engine

from lncrawl.dao import Chapter, ChapterImage, Job, Novel
from lncrawl.enums import JobType
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

    def test_removal_progress_is_answered_without_counting_again(self) -> None:
        from lncrawl.services import unfinished

        with (
            patch.object(unfinished, "find", side_effect=AssertionError("counted")),
            patch.object(unfinished, "status", return_value={"running": True, "done": 1}),
        ):
            self.assertEqual(
                novels_api.unfinished_novels(progress_only=True),
                {"removing": {"running": True, "done": 1}},
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


class JobListTests(unittest.TestCase):
    def setUp(self) -> None:
        import test_novel_repair

        fixture = test_novel_repair.NovelRepairTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.ctx = fixture.ctx
        self.engine = fixture.engine
        with Session(self.engine) as session:
            for n, done in enumerate([True, False, False]):
                session.add(
                    Job(
                        id=f"job-{n}",
                        user_id="user",
                        type=JobType.NOVEL,
                        is_done=done,
                        extra={"url": f"https://example.com/{n}"},
                    )
                )
            session.commit()

    def ids(self, **filters) -> list:
        return sorted(job.id for job in self.ctx.jobs.list(limit=10, **filters).items)

    def test_the_list_leaves_out_the_long_lists_one_job_still_has(self) -> None:
        heavy = {
            "urls": ["https://example.com/1"],
            "discovered": ["a"],
            "completeness": {"total": 1},
            "domain": "example.com",
            "url": "https://example.com/novel",
        }
        with Session(self.engine) as session:
            session.add(Job(id="heavy", user_id="user", type=JobType.NOVEL, extra=dict(heavy)))
            session.commit()
        client = self.fixture.client
        listed = client.get("/api/jobs?limit=10").json()["items"]
        row = next(job for job in listed if job["id"] == "heavy")
        self.assertEqual(
            row["extra"], {"domain": "example.com", "url": "https://example.com/novel"}
        )
        self.assertEqual(client.get("/api/job/heavy").json()["extra"], heavy)
        with Session(self.engine) as session:
            self.assertEqual(session.get(Job, "heavy").extra, heavy)

    def test_is_done_false_lists_the_unfinished_jobs(self) -> None:
        self.assertEqual(self.ids(is_done=False), ["job-1", "job-2"])
        self.assertEqual(self.ids(is_done=True), ["job-0"])
        self.assertEqual(self.ids(), ["job-0", "job-1", "job-2"])


class SourceListCachingTests(unittest.TestCase):
    def setUp(self) -> None:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        # the package names its router `sources`, so the module is asked for by path
        sources_api = importlib.import_module("lncrawl.server.api.sources")
        self.counts = {"example.com": 1}

        class Item(SimpleNamespace):
            def model_dump(self) -> dict:
                return dict(vars(self))

        ctx = SimpleNamespace(
            activity=SimpleNamespace(record=lambda *args: None),
            novels=SimpleNamespace(list_domains=lambda: dict(self.counts)),
            sources=SimpleNamespace(
                list=lambda include_rejected: [Item(domain="example.com", total_novels=0)]
            ),
        )
        patcher = patch.object(sources_api, "ctx", ctx)
        patcher.start()
        self.addCleanup(patcher.stop)
        app = FastAPI()
        app.include_router(sources_api.router, prefix="/api/source")
        app.dependency_overrides[sources_api.ensure_user] = lambda: SimpleNamespace(id="user")
        self.client = TestClient(app)

    def test_an_unchanged_list_is_not_sent_again_and_new_counts_are(self) -> None:
        first = self.client.get("/api/sources")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()[0]["total_novels"], 1)
        self.assertNotIn("max-age", first.headers["cache-control"])
        etag = first.headers["etag"]
        again = self.client.get("/api/sources", headers={"If-None-Match": etag})
        self.assertEqual(again.status_code, 304)
        self.counts["example.com"] = 2  # a novel was downloaded
        changed = self.client.get("/api/sources", headers={"If-None-Match": etag})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()[0]["total_novels"], 2)


class ReadAheadTests(unittest.TestCase):
    def setUp(self) -> None:
        import test_novel_repair

        fixture = test_novel_repair.NovelRepairTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.ctx = fixture.ctx
        self.user = fixture.user
        self.ctx.history = SimpleNamespace(add=MagicMock())
        self.ctx.tier.auto_fetch_enabled = lambda user: False

    def test_a_chapter_fetched_ahead_is_not_read_yet(self) -> None:
        response = self.ctx.chapters.read(self.user, "chapter-1", add_to_history=False)
        self.assertEqual(response.content, "<p>Existing chapter one.</p>")
        self.ctx.history.add.assert_not_called()

    def test_a_chapter_opened_is_read(self) -> None:
        self.ctx.chapters.read(self.user, "chapter-1")
        self.ctx.history.add.assert_called_once()


if __name__ == "__main__":
    unittest.main()
