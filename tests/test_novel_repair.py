from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from zipfile import ZipFile

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from lncrawl.core import Crawler
from lncrawl.dao import Chapter, Job, Novel, User, Volume
from lncrawl.enums import JobPriority, JobStatus
from lncrawl.server.api.novels import router
from lncrawl.server.security import ensure_user
from lncrawl.services.artifacts import ArtifactService
from lncrawl.services.binder.service import BinderService
from lncrawl.services.chapters import ChapterService
from lncrawl.services.crawler import CrawlerService
from lncrawl.services.file import FileService
from lncrawl.services.images import ChapterImageService
from lncrawl.services.jobs.service import JobService
from lncrawl.services.novels import NovelService
from lncrawl.services.scheduler.handlers import run_job
from lncrawl.services.volumes import VolumeService


class NovelRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(TemporaryDirectory()))
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
        self.addCleanup(self.engine.dispose)
        SQLModel.metadata.create_all(self.engine)
        self.user = User(id="user", email="reader@example.com", password="test-only")
        self.novel = Novel(
            id="novel",
            title="Test Novel",
            url="https://novelfire.net/book/example",
            domain="novelfire.net",
            chapter_count=4,
        )
        volume = Volume(id="volume", novel_id="novel", serial=1, title="Volume 1")
        with Session(self.engine, expire_on_commit=False) as session:
            session.add_all([self.user, self.novel, volume])
            session.add_all(
                [
                    Chapter(
                        id=f"chapter-{i}",
                        novel_id="novel",
                        volume_id="volume",
                        serial=i,
                        title=f"Chapter {i}",
                        url=f"https://novelfire.net/book/example/chapter-{i}",
                        is_done=i in (1, 2),
                        extra={"crawler_version": 1},
                    )
                    for i in range(1, 5)
                ]
            )
            session.commit()
        self.ctx = SimpleNamespace(
            config=SimpleNamespace(
                app=SimpleNamespace(app_dir=root), crawler=SimpleNamespace(ignore_images=True)
            ),
            db=SimpleNamespace(session=lambda: Session(self.engine, expire_on_commit=False)),
            users=SimpleNamespace(get=lambda user_id: self.user),
            tier=SimpleNamespace(
                max_active_jobs=lambda user: None,
                max_active_search_jobs=lambda user: None,
                job_priority=lambda user: JobPriority.NORMAL,
            ),
            logger=SimpleNamespace(info=MagicMock(), debug=MagicMock()),
            activity=SimpleNamespace(record=MagicMock()),
            job_notifier=SimpleNamespace(notify=MagicMock()),
            files=FileService(),
            chapters=ChapterService(),
            crawler=CrawlerService(),
            jobs=JobService(),
            novels=NovelService(),
            volumes=VolumeService(),
            images=ChapterImageService(),
            binder=BinderService(),
            artifacts=ArtifactService(),
        )
        self.source = SimpleNamespace(
            version=1,
            scraper=SimpleNamespace(signal=Event()),
            close=MagicMock(),
            _extract_images=lambda chapter: None,
        )
        self.downloads = []

        def download(chapter):
            self.downloads.append(chapter.id)
            chapter.body = f"<p>Recovered story text for chapter {chapter.id}.</p>"

        self.source.download_chapter = download
        self.source.format_chapter = lambda chapter: Crawler.format_chapter(self.source, chapter)
        self.ctx.sources = SimpleNamespace(init_crawler=MagicMock(return_value=self.source))
        modules = [
            "dao.chapter",
            "dao.novel",
            "dao.chapter_image",
            "dao.artifact",
            "services.file",
            "services.chapters",
            "services.crawler",
            "services.jobs.service",
            "services.novels",
            "services.volumes",
            "services.images",
            "services.artifacts",
            "services.binder.service",
            "services.binder.epub",
            "server.api.novels",
            "services.scheduler.handlers._base",
            "services.scheduler.handlers.fetch_missing",
            "services.scheduler.handlers.chapter_batch",
            "services.scheduler.handlers.chapter",
            "services.scheduler.handlers.artifact",
        ]
        for module in modules:
            self.stack.enter_context(patch(f"lncrawl.{module}.ctx", self.ctx))
        self.ctx.files.save_text("novels/novel/chapters/000001.zst", "<p>Existing chapter one.</p>")
        self.ctx.files.save_text("novels/novel/chapters/000002.zst", "<p>&nbsp;</p>")
        app = FastAPI()
        app.include_router(router, prefix="/api/novel", dependencies=[Depends(ensure_user)])
        app.dependency_overrides[ensure_user] = lambda: self.user
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def drain_jobs(self) -> None:
        signal = MagicMock(spec=Event)
        signal.is_set.return_value = False
        signal.wait.return_value = False
        for _ in range(100):
            job = self.ctx.jobs._pending()
            if job is None:
                return
            run_job(job, signal)
        self.fail("Repair job did not finish")

    def repair(self) -> Job:
        response = self.client.post("/api/novel/novel/repair")
        self.assertEqual(response.status_code, 200, response.text)
        job_id = response.json()["id"]
        self.drain_jobs()
        return self.ctx.jobs.get(job_id)

    def test_completeness_distinguishes_actual_files_from_stale_flags(self) -> None:
        report = self.client.get("/api/novel/novel/completeness")
        self.assertEqual(report.status_code, 200)
        data = report.json()
        self.assertEqual(
            (data["downloaded"], data["missing"], data["failed"], data["empty"]), (1, 2, 0, 1)
        )
        self.assertFalse(data["complete"])
        self.assertEqual([gap["serial"] for gap in data["gaps"]], [2, 3, 4])

    def test_repair_downloads_only_gaps_and_rebuilds_real_epub(self) -> None:
        job = self.repair()
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        self.assertCountEqual(self.downloads, [2, 3, 4])
        self.assertEqual(self.ctx.sources.init_crawler.call_count, 3)
        self.assertTrue(job.extra["completeness"]["complete"])
        artifact = self.ctx.artifacts.get(job.extra["artifact_id"])
        with ZipFile(self.ctx.files.resolve(artifact.output_file)) as book:
            for serial in range(1, 5):
                chapter_file = next(
                    name for name in book.namelist() if name.endswith(f"chapter_{serial:05}.xhtml")
                )
                text = book.read(chapter_file)
                expected = (
                    b"Existing chapter one."
                    if serial == 1
                    else f"Recovered story text for chapter {serial}.".encode()
                )
                self.assertIn(expected, text)
        self.downloads.clear()
        self.repair()
        self.assertEqual(self.downloads, [])

    def test_failed_download_propagates_and_existing_epub_is_preserved(self) -> None:
        successful_download = self.source.download_chapter
        old_epub = self.ctx.files.save("novels/novel/artifacts/Test Novel.epub", b"existing ebook")

        def download(chapter):
            self.downloads.append(chapter.id)
            raise ValueError("Source unavailable")

        self.source.download_chapter = download
        job = self.repair()
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.extra["completeness"]["failed"], 2)
        self.assertEqual(job.extra["completeness"]["empty"], 1)
        self.assertNotIn("artifact_id", job.extra)
        self.assertEqual(old_epub.read_bytes(), b"existing ebook")
        self.assertEqual(
            self.ctx.chapters.get("chapter-3").extra["download_error"], "Source unavailable"
        )
        self.source.download_chapter = successful_download
        repaired = self.repair()
        self.assertEqual(repaired.status, JobStatus.SUCCESS, repaired.error)
        self.assertTrue(repaired.extra["completeness"]["complete"])
        self.assertNotIn("download_error", self.ctx.chapters.get("chapter-3").extra)

    def test_empty_downloads_stay_empty_and_do_not_create_epub(self) -> None:
        self.source.download_chapter = lambda chapter: setattr(chapter, "body", "<div></div>")
        job = self.repair()
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertEqual(job.extra["completeness"]["empty"], 3)
        self.assertNotIn("artifact_id", job.extra)

    def test_canceled_repair_is_not_scheduled(self) -> None:
        job = self.ctx.jobs.fetch_missing_chapters(self.user, "novel", rebuild_epub=True)
        self.ctx.jobs.cancel(job.id)
        self.assertIsNone(self.ctx.jobs._pending())
        self.assertEqual(self.ctx.jobs.get(job.id).status, JobStatus.CANCELED)

    def test_zero_chapters_are_not_reported_complete(self) -> None:
        with Session(self.engine) as session:
            session.add(
                Novel(
                    id="empty", title="Empty", url="https://example.com/empty", domain="example.com"
                )
            )
            session.commit()
        report = self.client.get("/api/novel/empty/completeness").json()
        self.assertEqual(report["total"], 0)
        self.assertFalse(report["complete"])

    def test_unknown_novel_is_rejected(self) -> None:
        self.assertEqual(self.client.get("/api/novel/unknown/completeness").status_code, 404)


if __name__ == "__main__":
    unittest.main()
