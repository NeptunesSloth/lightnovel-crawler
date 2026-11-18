"""Real HTTP → NovelFire parser → SQLite → EPUB/ZIP integration checks."""

from collections import Counter
import errno
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

MB = 1024 * 1024
GB = 1024 * MB


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
        self.empty = set()  # always answered without the story, so never downloaded
        self.fail_once = set()
        self.js_redirected = set()
        counts, slow, notices, empty = self.counts, self.slow, self.notices, self.empty
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
                if self.path in empty or (self.path in notices and counts[self.path] == 1):
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

    def export(self, seconds=2, urls=None, **options):
        module = "lncrawl.services.scheduler.handlers.export_source"
        job = self.ctx.jobs._create(
            self.user,
            JobType.EXPORT_SOURCE,
            {
                "domain": "fixture.test",
                "urls": urls or [self.novel_url],
                "retries": 0,
                "dedupe": False,
                "auto_tune": False,
                "requests_per_sec": 100,
                **options,
            },
        )
        with (
            patch(f"{module}.ctx", self.ctx),
            patch(f"{module}._copy_to_desktop", return_value=None),
            patch(f"{module}.ExportSourceHandler._notify_finish"),
            patch(
                "lncrawl.services.crawler.request_budget",
                side_effect=lambda signal, **_: request_budget(signal, seconds),
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

    def test_progress_is_counted_in_chapters(self) -> None:
        job = self.export(library_only=True)
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        self.assertEqual(
            self.ctx.chapters.counts_by_url([self.novel_url]), {self.novel_url: (4, 4)}
        )
        self.assertEqual(job.extra["chapters_total"], 4)
        self.assertEqual(job.extra["chapters_done"], 4)
        self.assertEqual(job.extra["novels_unlisted"], 0)

    def test_a_second_run_does_not_read_the_saved_chapters_again(self) -> None:
        self.assertEqual(self.export(library_only=True).status, JobStatus.SUCCESS)
        from lncrawl.services.file import FileService

        real = FileService.has_chapter_content
        read = []

        def counted(files, path):
            read.append(path)
            return real(files, path)

        with patch.object(FileService, "has_chapter_content", counted):
            again = self.export(library_only=True)
        self.assertEqual(again.status, JobStatus.SUCCESS, again.error)
        self.assertEqual(read, [])

    def test_zip_is_written_only_to_the_chosen_folder(self) -> None:
        chosen = self.root / "roomier-drive" / "novels"
        job = self.export(save_dir=str(chosen))
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        saved = chosen / "fixture.test-novels.zip"
        self.assertEqual(job.extra["export_file"], str(saved))
        self.assertEqual(job.extra["saved_to"], str(saved))
        self.assertEqual([path.name for path in chosen.iterdir()], [saved.name])  # no .part
        with ZipFile(saved) as bundle:
            self.assertTrue(any(name.endswith(".epub") for name in bundle.namelist()))
        # no second copy in the data folder
        self.assertFalse(self.ctx.files.resolve(f"exports/{job.id}.zip").exists())

    def test_complete_only_keeps_a_fully_downloaded_novel_in_the_zip(self) -> None:
        job = self.export(complete_only=True)
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        self.assertEqual(job.extra["complete"], 1)
        self.assertEqual(job.extra.get("left_out"), 0)
        with ZipFile(self.ctx.files.resolve(job.extra["export_file"])) as bundle:
            self.assertTrue(any(name.endswith(".epub") for name in bundle.namelist()))

    def test_complete_only_leaves_a_partly_downloaded_novel_out_of_the_zip(self) -> None:
        self.empty.add("/chapter-4")  # one chapter never comes
        partial = self.export()
        self.assertEqual(partial.status, JobStatus.SUCCESS, partial.error)
        self.assertEqual(partial.extra["incomplete"], 1)  # by default it goes in, marked partial
        job = self.export(complete_only=True)
        self.assertNotIn("export_file", job.extra)  # nothing fully downloaded: no zip
        self.assertEqual(job.extra["left_out"], 1)
        self.assertIn("fully downloaded", job.error or "")
        # still downloaded as far as it goes, in the library
        self.assertEqual(self.ctx.chapters.completeness("novel").downloaded, 3)

    def not_in_library(self) -> None:
        """The fixture's novel is in the library from the start; a site's novels mostly are not."""
        self.ctx.recommendations = MagicMock()  # told when a novel goes; not under test
        self.ctx.novels.delete("novel")
        self.assertIsNone(self.ctx.novels.find_by_url(self.novel_url))

    def test_zip_only_makes_the_zip_and_keeps_nothing_in_the_library(self) -> None:
        self.not_in_library()
        job = self.export(zip_only=True)
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        self.assertEqual(job.extra["complete"], 1)
        with ZipFile(self.ctx.files.resolve(job.extra["export_file"])) as bundle:
            self.assertTrue(any(name.endswith(".epub") for name in bundle.namelist()))
        self.assertIsNone(self.ctx.novels.find_by_url(self.novel_url))
        self.assertEqual(list(self.ctx.files.resolve("novels").glob("*/chapters")), [])
        self.assertEqual(list(self.ctx.files.resolve("exports/zip-only").glob("*")), [])
        # run again, the book is found in the zip: nothing is downloaded twice
        previous_requests = self.counts.copy()
        again = self.export(zip_only=True)
        self.assertEqual(again.status, JobStatus.SUCCESS, again.error)
        self.assertEqual(again.extra["resumed"], 1)
        self.assertEqual(self.counts, previous_requests)
        self.assertIsNone(self.ctx.novels.find_by_url(self.novel_url))

    def test_zip_only_leaves_a_novel_that_was_in_the_library_before(self) -> None:
        job = self.export(zip_only=True)
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        self.assertEqual(job.extra["complete"], 1)
        self.assertIsNotNone(self.ctx.novels.find_by_url(self.novel_url))
        self.assertEqual(self.ctx.chapters.completeness("novel").downloaded, 4)

    def test_zip_only_takes_a_partly_downloaded_novel_out_once_the_zip_is_made(self) -> None:
        self.not_in_library()
        self.empty.add("/chapter-4")  # one chapter never comes
        job = self.export(zip_only=True)
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        self.assertEqual(job.extra["incomplete"], 1)
        with ZipFile(self.ctx.files.resolve(job.extra["export_file"])) as bundle:
            self.assertTrue(any(name.endswith(".epub") for name in bundle.namelist()))
        self.assertIsNone(self.ctx.novels.find_by_url(self.novel_url))

    def test_a_zip_only_export_that_is_stopped_keeps_what_it_has_for_the_resume(self) -> None:
        self.not_in_library()
        self.empty.add("/chapter-4")
        job = self.export(zip_only=True, complete_only=True)  # nothing whole: no zip is made
        self.assertNotIn("export_file", job.extra)
        self.assertIsNotNone(self.ctx.novels.find_by_url(self.novel_url))
        self.assertEqual(job.extra["zip_only_added"], [self.novel_url])

    def test_a_folder_that_cannot_be_written_stops_the_export_before_any_download(self) -> None:
        not_a_folder = self.root / "file.txt"
        not_a_folder.write_text("x")
        job = self.export(save_dir=str(not_a_folder))
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertIn("Cannot save the zip to", job.error or "")
        self.assertEqual(sum(self.counts.values()), 0)

    def test_a_full_disk_holds_the_export_until_there_is_room(self) -> None:
        # 7 MB left on a user's drive: chapters were fetched and then could not
        # be saved, so the export ran on at under a chapter a minute, saying
        # only "loading its chapter list".
        module = "lncrawl.services.scheduler.handlers.export_source"
        free = iter([7 * MB, 7 * MB])  # then plenty
        waits = []

        def wait(handler, seconds, phase, **extra):
            waits.append((phase, sum(self.counts.values()), extra.get("disk_free")))

        with (
            patch(f"{module}._free_space", side_effect=lambda: next(free, 10 * GB)),
            patch(f"{module}.ExportSourceHandler._wait", wait),
        ):
            job = self.export(until_done=True, library_only=True, keep_awake=False)
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        self.assertTrue(self.ctx.chapters.completeness("novel").complete)
        held = [w for w in waits if w[0] == "disk-full"]
        self.assertEqual(len(held), 2)
        self.assertEqual(held[0][1], 0)  # nothing asked of the site while there was no room
        self.assertEqual(held[0][2], 7 * MB)  # and how much room there was, to show

    def test_a_novel_that_would_not_fit_is_not_started_until_there_is_room(self) -> None:
        # Ask before it starts, not after the drive is at 0 MB: there is still
        # room then to move the library somewhere bigger.
        module = "lncrawl.services.scheduler.handlers.export_source"
        free = iter([10 * GB, 2 * GB, 2 * GB])  # room to start; not for this novel; then plenty
        waits = []

        def wait(handler, seconds, phase, **extra):
            chapters = sum(n for path, n in self.counts.items() if path.startswith("/chapter"))
            waits.append((phase, chapters, extra))

        with (
            patch(f"{module}._free_space", side_effect=lambda: next(free, 100 * GB)),
            patch(f"{module}.ExportSourceHandler._wait", wait),
        ):
            # four chapters at a gigabyte each, going by the novels before it
            job = self.export(
                until_done=True, library_only=True, keep_awake=False, bytes_per_chapter=GB
            )
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        self.assertTrue(self.ctx.chapters.completeness("novel").complete)
        held = [w for w in waits if w[0] == "disk-full"]
        self.assertTrue(held)
        self.assertEqual(held[0][1], 0)  # none of its chapters was fetched first
        self.assertEqual(held[0][2]["disk_title"], "Test Novel")
        self.assertGreater(held[0][2]["disk_need"], 4 * GB)
        self.assertEqual(held[0][2]["disk_free"], 2 * GB)
        # and what this novel really took is what the next one is judged by
        self.assertLess(job.extra["bytes_per_chapter"], MB)

    def test_without_until_done_it_stops_and_says_what_is_needed(self) -> None:
        module = "lncrawl.services.scheduler.handlers.export_source"
        with patch(f"{module}._free_space", return_value=2 * GB):
            job = self.export(library_only=True, bytes_per_chapter=GB)
        self.assertEqual(job.status, JobStatus.FAILED)
        self.assertIn("Stopped before the drive filled up", job.error or "")
        self.assertIn("Test Novel needs about 5.0 GB", job.error or "")
        self.assertFalse(any(path.startswith("/chapter") for path in self.counts))

    def test_a_novel_that_fits_is_not_held_up(self) -> None:
        module = "lncrawl.services.scheduler.handlers.export_source"
        with patch(f"{module}._free_space", return_value=100 * GB):
            job = self.export(library_only=True, bytes_per_chapter=GB)
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)

    def test_a_disk_that_fills_up_part_way_stops_the_novel_and_waits(self) -> None:
        module = "lncrawl.services.scheduler.handlers.export_source"
        files = type(self.ctx.files)
        save_text = files.save_text
        full = [1]

        def fill_up(store, path, content, *args, **kwargs):
            if "/chapters/" in str(path) and full[0]:
                full[0] -= 1
                raise OSError(errno.ENOSPC, "No space left on device")
            return save_text(store, path, content, *args, **kwargs)

        waits = []
        with (
            patch.object(files, "save_text", fill_up),
            patch(f"{module}._free_space", return_value=10 * GB),
            patch(
                f"{module}.ExportSourceHandler._wait", lambda h, s, phase, **e: waits.append(phase)
            ),
        ):
            job = self.export(until_done=True, library_only=True, keep_awake=False)
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        self.assertIn("disk-full", waits)
        self.assertTrue(self.ctx.chapters.completeness("novel").complete)

    def test_unattended_library_download_over_real_http(self) -> None:
        job = self.export(until_done=True, library_only=True, keep_awake=False)
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        self.assertEqual(job.extra["complete"], 1)
        self.assertNotIn("export_file", job.extra)
        self.assertTrue(self.ctx.chapters.completeness("novel").complete)

    def test_two_novels_download_at_once_on_their_own_crawlers(self) -> None:
        made = []
        init = self.ctx.sources.init_crawler

        def counted(*args, **kwargs):
            made.append(1)
            return init(*args, **kwargs)

        self.ctx.sources.init_crawler = counted
        job = self.export(urls=[self.novel_url, self.novel_url + "-gone"])
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        self.assertEqual(len(made), 2)  # a second lane was opened
        self.assertEqual(job.extra["complete"], 1)
        self.assertEqual(job.extra["failed"], 1)
        self.assertIn("active_novels", job.extra)

    def test_the_pause_between_novels_says_why_and_ends_when_the_next_one_starts(self) -> None:
        # The page said "Waiting 3 seconds ... after source errors" for over a
        # minute: the pause was polite mode's, and the phase stayed set while
        # the next novel's chapter list loaded.
        module = "lncrawl.services.scheduler.handlers.export_source"
        from lncrawl.services.scheduler.handlers.export_source import ExportSourceHandler

        updates: list = []
        original = ExportSourceHandler._set_extra

        def record(handler, **values):
            updates.append(values)
            return original(handler, **values)

        with (
            patch(f"{module}.POLITE_DELAY", 0.01),
            patch.object(ExportSourceHandler, "_set_extra", record),
        ):
            job = self.export(
                urls=[self.novel_url, self.novel_url + "-gone"], polite=True, parallel_novels=1
            )
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        pauses = [i for i, u in enumerate(updates) if u.get("phase") == "waiting-between-novels"]
        self.assertTrue(pauses)
        self.assertEqual(updates[pauses[0]].get("pause_reason"), "polite")
        after = next(u for u in updates[pauses[0] + 1 :] if "phase" in u)
        self.assertEqual(after["phase"], "downloading")
        self.assertIn("Example Gone", after["current_title"])  # named before its details load

    def test_the_rate_asked_for_survives_the_run_and_a_stale_one_is_let_go(self) -> None:
        asked = self.export(auto_tune=True, requests_per_sec=50)
        self.assertEqual(asked.status, JobStatus.SUCCESS, asked.error)
        self.assertEqual(asked.extra["requests_per_sec"], 50)  # still the cap on resume
        self.assertIn("current_rps", asked.extra)  # the tuner's own, kept apart
        # a job from before the split: its stored rate is where the tuner got to
        older = self.export(auto_tune=True, requests_per_sec=50, found_by_discovery=False)
        self.assertEqual(older.status, JobStatus.SUCCESS, older.error)
        self.assertEqual(older.extra["requests_per_sec"], 0)
        self.assertEqual(older.extra["current_rps"], 50)

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

    def test_redirected_chapters_go_straight_to_the_mirror_once_learned(self) -> None:
        from lncrawl.core import Chapter as CrawlerChapter
        from sources.en.n import novelfire

        self.addCleanup(setattr, novelfire, "_MIRRORS", novelfire._MIRRORS)
        novelfire._MIRRORS = novelfire._MirrorMap()
        origin = self.novel_url.split("/book/", 1)[0]
        self.js_redirected.update(f"/chapter-{serial}" for serial in range(1, 7))
        for serial in range(1, 7):
            chapter = CrawlerChapter(id=serial, url=f"{origin}/chapter-{serial}", title="")
            body = self.source.download_chapter_body(chapter)
            self.assertIn(f"Real story text for chapter {serial}.", body)
        # chapters 1 and 2 go through the redirect chain and agree on where it
        # ends; from chapter 3 on, one request to the mirror each
        for serial in (1, 2):
            self.assertEqual(self.counts[f"/chapter-{serial}"], 1)
            self.assertEqual(self.counts[f"/loading/chapter-{serial}"], 1)
        for serial in range(3, 7):
            self.assertEqual(self.counts[f"/chapter-{serial}"], 0)
            self.assertEqual(self.counts[f"/loading/chapter-{serial}"], 0)
            self.assertEqual(self.counts[f"/mirror/chapter-{serial}?noti=1"], 1)

    def test_a_mirror_that_stops_serving_falls_back_to_the_chain(self) -> None:
        from lncrawl.core import Chapter as CrawlerChapter
        from sources.en.n import novelfire

        self.addCleanup(setattr, novelfire, "_MIRRORS", novelfire._MIRRORS)
        novelfire._MIRRORS = novelfire._MirrorMap()
        origin = self.novel_url.split("/book/", 1)[0]
        self.js_redirected.update(f"/chapter-{serial}" for serial in range(1, 9))
        # the mirror only gives the login notice when asked directly
        self.notices.update(f"/mirror/chapter-{serial}?noti=1" for serial in range(3, 9))
        for serial in range(1, 9):
            chapter = CrawlerChapter(id=serial, url=f"{origin}/chapter-{serial}", title="")
            self.source.download_chapter_body(chapter)
        # each direct miss went back through the chain; after three in a row the
        # mirror is not asked directly any more
        for serial in range(3, 6):
            self.assertEqual(self.counts[f"/chapter-{serial}"], 1)
        for serial in range(6, 9):
            self.assertEqual(self.counts[f"/chapter-{serial}"], 1)
            self.assertEqual(self.counts[f"/mirror/chapter-{serial}?noti=1"], 1)

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
