"""Real HTTP → NovelFire parser → SQLite → EPUB/ZIP integration checks."""

from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Event, Thread
from time import sleep
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from zipfile import ZipFile

from PIL import Image
from sqlmodel import Session, create_engine
import test_novel_repair

from lncrawl.core import scraping
from lncrawl.core.request_budget import request_budget
from lncrawl.dao import Chapter, Novel
from lncrawl.enums import JobStatus, JobType
from lncrawl.services.scheduler.handlers import run_job
from sources.en.n.novelfire import NovelFireCrawler


class HttpCrawlTests(unittest.TestCase):
    def setUp(self) -> None:
        # Reuse the existing real database/service fixture, not its fake
        # downloader. This test replaces that downloader with the actual source.
        fixture = test_novel_repair.NovelRepairTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.ctx = fixture.ctx
        self.root = self.ctx.config.app.app_dir
        disk_database = self.root / "http-crawl.sqlite"
        connection = fixture.engine.raw_connection()
        try:
            with sqlite3.connect(disk_database) as target:
                connection.driver_connection.backup(target)
        finally:
            connection.close()
        self.engine = create_engine(
            f"sqlite:///{disk_database}", connect_args={"check_same_thread": False}
        )
        self.addCleanup(self.engine.dispose)
        self.ctx.db = SimpleNamespace(session=lambda: Session(self.engine, expire_on_commit=False))
        self.user = fixture.user
        self.counts = Counter()
        self.slow = set()
        self.notices = set()
        self.fail_once = set()
        self.js_redirected = set()
        counts, slow, notices = self.counts, self.slow, self.notices
        fail_once, js_redirected = self.fail_once, self.js_redirected
        cover = BytesIO()
        Image.new("RGB", (2, 2), "blue").save(cover, format="JPEG")
        cover_bytes = cover.getvalue()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                counts[self.path] += 1
                if self.path in slow:
                    sleep(1.2)
                if self.path in js_redirected:
                    # NovelFire's chapter pages answer with a redirect to a "Loading..."
                    # page whose script sends the reader on to a mirror holding the text.
                    self.send_response(302)
                    self.send_header("Location", f"/loading{self.path}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path.startswith("/loading/"):
                    origin = f"http://127.0.0.1:{self.server.server_port}"
                    mirror = f"{origin}/mirror{self.path.removeprefix('/loading')}?noti=1"
                    # The page writes the address as a JSON string, slashes escaped.
                    escaped = mirror.replace("/", "\\/")
                    body = (
                        "<!DOCTYPE html><html><head><title>Loading...</title></head><body>"
                        '<div class="text">Loading...</div><script>setTimeout(() => {'
                        f' window.location.href = "{escaped}"; }}, 5);</script></body></html>'
                    ).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                serial = self.path.split("?", 1)[0].rsplit("-", 1)[-1]
                body = (
                    f'<html><body><div id="content"><div class="chapter-text">'
                    f"<h2>Chapter {serial}: A title</h2><p>Real story text for chapter {serial}.</p>"
                    "</div></div></body></html>"
                ).encode()
                if self.path in notices and counts[self.path] == 1:
                    from lncrawl.utils.chapter_content import NOVELFIRE_LOGIN_NOTICE

                    body = f'<div id="content"><p>{NOVELFIRE_LOGIN_NOTICE}</p></div>'.encode()
                content_type = "text/html; charset=utf-8"
                if self.path == "/book/example":
                    body = (
                        '<h1>Test Novel</h1><span itemprop="author">Fixture Author</span>'
                        '<div class="cover"><img src="/cover.jpg"></div>'
                        '<div class="summary"><div class="content">Fixture synopsis.</div></div>'
                    ).encode()
                elif self.path.startswith("/book/example/chapters"):
                    serials = (3, 4) if "page=2" in self.path else (1, 2)
                    body = (
                        '<ul class="chapter-list">'
                        + "".join(
                            f'<li><a title="Chapter {number}" href="/chapter-{number}">Chapter {number}</a></li>'
                            for number in serials
                        )
                        + "</ul>"
                        + (
                            ""
                            if "page=2" in self.path
                            else '<a class="page-link" rel="next" href="/book/example/chapters?page=2">Next</a>'
                        )
                    ).encode()
                elif self.path.startswith("/search?"):
                    body = (
                        '<ul class="novel-list"><li class="novel-item"><a title="Test Novel" href="/book/example">'
                        '<span class="icon-book-open">4</span><span class="icon-crown">1</span></a></li></ul>'
                    ).encode()
                elif self.path == "/cover.jpg":
                    content_type = "image/jpeg"
                    body = cover_bytes
                try:
                    self.send_response(
                        503 if self.path in fail_once and counts[self.path] == 1 else 200
                    )
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        origin = f"http://127.0.0.1:{self.server.server_port}/"
        self.novel_url = origin + "book/example"
        with self.ctx.db.session() as session:
            novel = session.get(Novel, "novel")
            novel.url = self.novel_url
            novel.synopsis = "A fixture novel with real HTTP chapters."
            session.add(novel)
            for serial in range(1, 5):
                chapter = session.get(Chapter, f"chapter-{serial}")
                chapter.url = origin + f"chapter-{serial}"
                session.add(chapter)
                self.ctx.files.resolve(chapter.content_file).unlink(missing_ok=True)
            session.commit()
        config = self.ctx.config.crawler
        config.requests_per_sec = 0
        config.enable_proxy = False
        config.can_use_browser = False
        self.ctx.logger.progress_bar = False
        self.ctx.logger.warn = MagicMock()
        self.ctx.logger.is_debug = False
        self.ctx.tags = SimpleNamespace(insert=MagicMock())
        for module in (
            "lncrawl.core.scraping",
            "lncrawl.core.crawler",
            "lncrawl.core.taskman",
            "lncrawl.core.template",
        ):
            fixture.stack.enter_context(patch(f"{module}.ctx", self.ctx))
        for name, value in [("_configs", {}), ("_states", {}), ("_memory", None)]:
            fixture.stack.enter_context(patch.object(scraping, name, value))
        fixture.stack.callback(scraping.close_shared_state)

        class Source(NovelFireCrawler):
            base_url = [origin]
            search_url = origin + "search?keyword={}&type=title"

        self.source = Source(workers=4, origin=origin)
        self.source.version = 1
        # Export closes its source at the end; each run gets a new real session.
        self.ctx.sources.get_source = MagicMock(return_value=SimpleNamespace(url=origin))
        self.ctx.sources.init_crawler = lambda *args, **kwargs: self.new_source(Source, origin)
        self.fixture = fixture

    def new_source(self, source_type, origin):
        source = source_type(workers=4, origin=origin)
        source.version = 1
        return source

    def export(self, seconds=2):
        module = "lncrawl.services.scheduler.handlers.export_source"
        job = self.ctx.jobs._create(
            self.user,
            JobType.EXPORT_SOURCE,
            {
                "domain": "fixture.test",
                "urls": [self.novel_url],
                "retries": 0,
                "dedupe": False,
                "auto_tune": False,
                "requests_per_sec": 100,
            },
        )
        with (
            patch(f"{module}.ctx", self.ctx),
            patch(f"{module}._copy_to_desktop", return_value=None),
            patch(f"{module}.ExportSourceHandler._notify_finish"),
            patch(
                "lncrawl.services.crawler.request_budget",
                side_effect=lambda signal: request_budget(signal, seconds),
            ),
            patch(f"{module}.ADAPTIVE_STEP", 0),
        ):
            run_job(job, Event())
        return self.ctx.jobs.get(job.id)

    def test_real_http_text_is_preserved_in_epub_and_zip_and_resume_uses_no_network(self) -> None:
        job = self.export()
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        self.assertEqual(job.extra["complete"], 1)
        with ZipFile(self.ctx.files.resolve(job.extra["export_file"])) as bundle:
            epub_name = next(name for name in bundle.namelist() if name.endswith(".epub"))
            with ZipFile(BytesIO(bundle.read(epub_name))) as epub:
                for serial in range(1, 5):
                    member = next(
                        name
                        for name in epub.namelist()
                        if name.endswith(f"chapter_{serial:05}.xhtml")
                    )
                    self.assertIn(
                        f"Real story text for chapter {serial}.".encode(), epub.read(member)
                    )
        previous_requests = self.counts.copy()
        resumed = self.export()
        self.assertEqual(resumed.status, JobStatus.SUCCESS, resumed.error)
        self.assertEqual(resumed.extra["resumed"], 1)
        self.assertEqual(self.counts, previous_requests)

    def test_real_novelfire_metadata_pagination_cover_and_discovery(self) -> None:
        urls = self.ctx.crawler.discover_novels(
            self.user.id,
            "fixture.test",
            Event(),
            seeds="ab",
            custom=self.source,
            use_sitemap=False,
            expand_seeds=False,
        )
        self.assertEqual(urls, [self.novel_url])
        novel = self.ctx.crawler.fetch_novel(self.user.id, self.novel_url, custom=self.source)
        self.assertEqual(novel.title, "Test Novel")
        self.assertEqual(novel.authors, "Fixture Author")
        self.assertEqual(novel.chapter_count, 4)
        self.assertEqual(novel.volume_count, 2)
        self.assertTrue(self.ctx.files.exists(novel.cover_file))
        self.assertEqual(self.counts["/book/example/chapters"], 1)
        self.assertEqual(self.counts["/book/example/chapters?page=2"], 1)
        job = self.export()
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        self.assertEqual(job.extra["complete"], 1)

    def test_cli_subprocess_downloads_real_http_novel_to_epub(self) -> None:
        data = self.root / "cli-subprocess"
        source_file = data / "sources/en/n/fixture.py"
        source_file.parent.mkdir(parents=True)
        origin = self.novel_url.split("/book/", 1)[0] + "/"
        source_file.write_text(
            "from sources.en.n.novelfire import NovelFireCrawler\n"
            "class FixtureNovelFireCrawler(NovelFireCrawler):\n"
            f"    base_url = [{origin!r}]\n"
        )
        # Avoid upstream index traffic; then run the actual CLI parser/command.
        entry = "from lncrawl.context import ctx; ctx.setup(sync_remote_index=False); from lncrawl import main; main()"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                entry,
                "-ll",
                "crawl",
                self.novel_url,
                "--first",
                "4",
                "--format",
                "epub",
                "--noin",
            ],
            env=dict(os.environ, LNCRAWL_DATA_PATH=str(data)),
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-4000:])
        ebooks = list(data.glob("novels/*/artifacts/*.epub"))
        self.assertEqual(len(ebooks), 1, (result.stdout + result.stderr)[-4000:])
        with ZipFile(ebooks[0]) as epub:
            for serial in range(1, 5):
                member = next(
                    name for name in epub.namelist() if name.endswith(f"chapter_{serial:05}.xhtml")
                )
                self.assertIn(f"Real story text for chapter {serial}.".encode(), epub.read(member))

    def test_bulk_128_chapters_and_cold_restart_resume(self) -> None:
        with self.ctx.db.session() as session:
            novel = session.get(Novel, "novel")
            novel.chapter_count = 128
            session.add(novel)
            origin = self.novel_url.split("/book/", 1)[0]
            session.add_all(
                [
                    Chapter(
                        id=f"chapter-{serial}",
                        novel_id="novel",
                        volume_id="volume",
                        serial=serial,
                        title=f"Chapter {serial}",
                        url=f"{origin}/chapter-{serial}",
                        extra={},
                    )
                    for serial in range(5, 129)
                ]
            )
            session.commit()
        job = self.export(seconds=5)
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        self.assertEqual(job.extra["complete"], 1)
        self.assertTrue(self.ctx.chapters.completeness("novel").complete)
        self.assertEqual(sum(self.counts.values()), 128)
        with ZipFile(self.ctx.files.resolve(job.extra["export_file"])) as bundle:
            epub_name = next(name for name in bundle.namelist() if name.endswith(".epub"))
            with ZipFile(BytesIO(bundle.read(epub_name))) as epub:
                chapter_files = [name for name in epub.namelist() if "/chapter_" in name]
                self.assertEqual(len(chapter_files), 128)
        previous_requests = self.counts.copy()
        self.engine.dispose()
        scraping.close_shared_state()
        resumed = self.export()
        self.assertEqual(resumed.status, JobStatus.SUCCESS, resumed.error)
        self.assertEqual(resumed.extra["resumed"], 1)
        self.assertEqual(self.counts, previous_requests)

    def test_all_chapter_downloads_fail_without_creating_empty_ebook(self) -> None:
        with (
            patch.object(
                self.ctx.crawler, "fetch_chapter", side_effect=RuntimeError("source blocked")
            ),
            patch.object(self.ctx.binder, "make_artifact") as binder,
        ):
            job = self.export()
        self.assertEqual(job.status, JobStatus.FAILED)
        binder.assert_not_called()
        self.assertFalse(list((self.root / "novels").rglob("*.epub")))

    def test_real_http_login_notice_is_retried_until_actual_story_is_saved(self) -> None:
        self.notices.add("/chapter-2")
        job = self.export(seconds=5)
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        self.assertEqual(job.extra["complete"], 1)
        self.assertEqual(self.counts["/chapter-2"], 2)
        chapter = self.ctx.chapters.get("chapter-2")
        self.assertIn(
            "Real story text for chapter 2.", self.ctx.files.load_text(chapter.content_file)
        )

    def test_chapter_behind_javascript_redirect_with_escaped_url_is_downloaded(self) -> None:
        self.js_redirected.update(f"/chapter-{serial}" for serial in range(1, 5))
        job = self.export(seconds=5)
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        self.assertEqual(job.extra["complete"], 1)
        for serial in range(1, 5):
            self.assertEqual(self.counts[f"/mirror/chapter-{serial}?noti=1"], 1)
            chapter = self.ctx.chapters.get(f"chapter-{serial}")
            self.assertIn(
                f"Real story text for chapter {serial}.",
                self.ctx.files.load_text(chapter.content_file),
            )

    def test_transient_http_failure_is_retried_before_accepting_chapter(self) -> None:
        self.fail_once.add("/chapter-2")
        job = self.export(seconds=5)
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        self.assertEqual(job.extra["complete"], 1)
        self.assertGreaterEqual(self.counts["/chapter-2"], 2)

    def test_slow_chapter_fails_without_stalling_export_and_resume_fetches_only_that_gap(
        self,
    ) -> None:
        self.slow.add("/chapter-2")
        job = self.export(seconds=0.5)
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        self.assertEqual(job.extra["incomplete"], 1)
        report = self.ctx.chapters.completeness("novel")
        self.assertEqual(report.downloaded, 3)
        self.assertEqual([gap.serial for gap in report.gaps], [2])
        self.slow.clear()
        previous_requests = self.counts.copy()
        resumed = self.export()
        self.assertEqual(resumed.extra["complete"], 1)
        for serial in (1, 3, 4):
            self.assertEqual(
                self.counts[f"/chapter-{serial}"], previous_requests[f"/chapter-{serial}"]
            )
        self.assertGreater(self.counts["/chapter-2"], previous_requests["/chapter-2"])

    def tearDown(self) -> None:
        self.source.close()


if __name__ == "__main__":
    unittest.main()
