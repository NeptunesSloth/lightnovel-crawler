"""A bulk download left alone for days: it waits out trouble and still finishes."""

import errno
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import sqlmodel as sq
import test_novel_repair

from lncrawl.dao import Job
from lncrawl.enums import JobStatus, JobType
from lncrawl.services.scheduler.handlers import export_source
from lncrawl.services.scheduler.handlers.export_source import (
    ExportSourceHandler,
    ExportStalledError,
)
from lncrawl.utils.keep_awake import keep_awake

MODULE = "lncrawl.services.scheduler.handlers.export_source"
URL = "https://novelfire.net/book/example"


class UnattendedExportTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = test_novel_repair.NovelRepairTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.ctx = fixture.ctx
        self.user = fixture.user
        for level in ("warn", "info", "debug"):
            if not hasattr(self.ctx.logger, level):
                setattr(self.ctx.logger, level, lambda *args, **kwargs: None)
        # no real waiting in tests
        for name, value in [
            ("COOLDOWNS", (0.0,) * 6),
            ("RETRY_BACKOFF", 0.0),
            ("STALL_COOLDOWN", 0.0),
            ("DISK_FULL_WAIT", 0.0),
            ("ADAPTIVE_STEP", 0.0),
        ]:
            fixture.stack.enter_context(patch(f"{MODULE}.{name}", value))

    def export(self, **options) -> Job:
        options.setdefault("until_done", True)
        options.setdefault("keep_awake", False)
        return self.fixture.export_novel(**options)

    def test_library_only_puts_chapters_in_the_library_without_a_zip(self) -> None:
        job = self.export(library_only=True)
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        self.assertTrue(job.extra["library_only"])
        self.assertEqual(job.extra["complete"], 1)
        self.assertNotIn("export_file", job.extra)
        self.assertTrue(self.ctx.chapters.completeness("novel").complete)

    def test_a_site_failing_for_several_rounds_is_waited_out(self) -> None:
        download = self.fixture.source.download_chapter
        tries = []

        def flaky(chapter):
            tries.append(chapter.id)
            if chapter.id == 2 and tries.count(2) <= 4:  # down for a while
                raise ValueError("503 Service Unavailable")
            download(chapter)

        self.fixture.source.download_chapter = flaky
        job = self.export(retries=0)  # the fixed retry count no longer applies
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        self.assertEqual(job.extra["complete"], 1)
        self.assertGreater(tries.count(2), 4)

    def test_a_chapter_that_never_comes_is_kept_partial_and_the_job_ends(self) -> None:
        download = self.fixture.source.download_chapter

        def gone(chapter):
            if chapter.id == 2:
                raise ValueError("404 Not Found")
            download(chapter)

        self.fixture.source.download_chapter = gone
        job = self.export()
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        self.assertEqual(job.extra["incomplete"], 1)

    def test_hung_downloads_get_fresh_downloaders_instead_of_failing(self) -> None:
        real = export_source._download_novel
        calls = []

        def hang_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise ExportStalledError("No crawl worker completed in 120 seconds")
            return real(*args, **kwargs)

        with patch(f"{MODULE}._download_novel", side_effect=hang_once):
            job = self.export()
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        self.assertEqual(job.extra["stall_recoveries"], 1)
        self.assertEqual(job.extra["complete"], 1)

    def test_without_until_done_a_hang_still_stops_the_job(self) -> None:
        with patch(f"{MODULE}._download_novel", side_effect=ExportStalledError("hung")):
            job = self.export(until_done=False)
        self.assertEqual(job.status, JobStatus.FAILED)

    def test_a_full_disk_is_waited_out(self) -> None:
        real = export_source._download_novel
        calls = []

        def full_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(*args, **kwargs)

        with patch(f"{MODULE}._download_novel", side_effect=full_once):
            job = self.export()
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        self.assertEqual(job.extra["complete"], 1)

    def test_discovery_cut_short_carries_on_after_the_first_downloads(self) -> None:
        looks = [([URL], True), ([URL, URL + "-two"], False)]
        budgets = []

        def discover(self, crawler, domain, budget, polite, use_sitemap):
            budgets.append(budget)
            return looks.pop(0)

        with patch.object(ExportSourceHandler, "_discover", discover):
            job = self.export(discover=True, check_discovery=False)
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        self.assertEqual(job.extra["urls"], [URL, URL + "-two"])
        self.assertTrue(job.extra["discovery_done"])
        self.assertEqual(budgets[1], budgets[0] * 2)

    def test_an_unexpected_error_restarts_from_the_checkpoint(self) -> None:
        real = ExportSourceHandler._run_once
        runs = []

        def flaky_run(handler):
            runs.append(1)
            if len(runs) == 1:
                raise RuntimeError("database is locked")
            return real(handler)

        with (
            patch.object(ExportSourceHandler, "_run_once", flaky_run),
            patch(f"{MODULE}.RESTART_WAIT", 0.0),
        ):
            job = self.export()
        self.assertEqual(job.status, JobStatus.SUCCESS, job.error)
        self.assertEqual(job.extra["restarts"], 1)

    def test_waiting_keeps_the_job_alive(self) -> None:
        job = self.ctx.jobs._create(self.user, JobType.EXPORT_SOURCE, {"domain": "x.com"})
        handler = ExportSourceHandler(job, Event())
        with (
            patch(f"{MODULE}.ctx", self.ctx),
            patch(f"{MODULE}.HEARTBEAT", 0.05),
            patch.object(ExportSourceHandler, "user", self.user),
        ):
            stamps = []
            original = handler._set_extra

            def record(**values):
                stamps.append(values.get("phase"))
                original(**values)

            handler._set_extra = record  # type: ignore[method-assign]
            handler._wait(0.3, "cooling-down")
        self.assertGreaterEqual(stamps.count("cooling-down"), 4)
        self.assertEqual(stamps[-1], "downloading")


class SchedulingTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = test_novel_repair.NovelRepairTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.ctx = fixture.ctx
        self.user = fixture.user
        self.ctx.config.crawler.runner_concurrency = 5

    def export_job(self, domain: str, running: bool = False) -> Job:
        job = self.ctx.jobs._create(self.user, JobType.EXPORT_SOURCE, {"domain": domain})
        if running:
            with self.ctx.db.session() as sess:
                sess.exec(
                    sq.update(Job)
                    .where(sq.col(Job.id) == job.id)
                    .values(status=JobStatus.RUNNING, done=1)
                )
                sess.commit()
        return job

    def test_different_sites_download_at_the_same_time(self) -> None:
        self.export_job("a.com", running=True)
        waiting = self.export_job("b.com")
        picked = self.ctx.jobs._pending(False)
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked.id, waiting.id)

    def test_a_site_gets_one_bulk_download_at_a_time(self) -> None:
        self.export_job("a.com", running=True)
        self.export_job("a.com")
        self.assertIsNone(self.ctx.jobs._pending(False))

    def test_a_download_still_stopping_holds_only_its_own_site(self) -> None:
        same = self.export_job("a.com")
        held = [("finishing-job", "a.com")]  # cancelled, its worker not yet out
        self.assertIsNone(self.ctx.jobs._pending(False, held_exports=held))
        other = self.export_job("b.com")
        picked = self.ctx.jobs._pending(False, held_exports=held)
        assert picked is not None
        self.assertEqual(picked.id, other.id)
        self.assertNotEqual(picked.id, same.id)

    def test_bulk_download_makes_one_job_per_site(self) -> None:
        known = {"novelfire.net", "other.net"}

        def get_source(domain: str) -> SimpleNamespace:
            if domain not in known:
                raise KeyError(domain)
            return SimpleNamespace(domain=domain, url=f"https://{domain}/", can_search=False)

        self.ctx.sources.get_domain = lambda url: url.split("/")[2]
        self.ctx.sources.get_source = get_source
        jobs = self.ctx.jobs.bulk_download(
            self.user,
            [
                "https://novelfire.net/book/a",
                "https://novelfire.net/book/b",
                "https://other.net/n/1",
                "https://unknown.org/x",
            ],
        )
        self.assertEqual(len(jobs), 2)
        by_site = {job.extra["domain"]: job.extra for job in jobs}
        self.assertEqual(len(by_site["novelfire.net"]["urls"]), 2)
        self.assertTrue(all(job.extra["until_done"] and job.extra["library_only"] for job in jobs))


class KeepAwakeTests(unittest.TestCase):
    def test_holding_the_computer_awake_never_breaks_a_job(self) -> None:
        with patch("lncrawl.utils.keep_awake._start", side_effect=OSError("no tool")):
            with keep_awake():
                pass
        with patch("lncrawl.utils.keep_awake.platform.system", return_value="Plan9"):
            with keep_awake():
                pass


if __name__ == "__main__":
    unittest.main()


class DownloadsPageTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = test_novel_repair.NovelRepairTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.ctx = fixture.ctx
        self.user = fixture.user

    def export_job(self, domain: str = "a.com", **extra) -> Job:
        return self.ctx.jobs._create(self.user, JobType.EXPORT_SOURCE, {"domain": domain, **extra})

    def test_a_paused_download_is_not_started_until_resumed(self) -> None:
        job = self.export_job()
        self.ctx.jobs.pause(job.id)
        self.assertEqual(self.ctx.jobs.get(job.id).status, JobStatus.PAUSED)
        self.assertIsNone(self.ctx.jobs._pending(False))
        self.ctx.jobs.resume(job.id)
        picked = self.ctx.jobs._pending(False)
        assert picked is not None
        self.assertEqual(picked.id, job.id)

    def test_pausing_a_running_download_leaves_it_paused_and_quiet(self) -> None:
        job = self.export_job("novelfire.net", urls=["https://novelfire.net/book/example"])
        self.ctx.jobs.pause(job.id)
        signal = Event()
        signal.set()  # what Pause does to the worker
        handler = ExportSourceHandler(self.ctx.jobs.get(job.id), signal)
        with (
            patch(f"{MODULE}.ctx", self.ctx),
            patch.object(ExportSourceHandler, "_notify_finish") as notify,
            patch.object(
                ExportSourceHandler, "_run_once", side_effect=export_source.AbortedException()
            ),
        ):
            self.assertFalse(handler.process())
        self.assertEqual(self.ctx.jobs.get(job.id).status, JobStatus.PAUSED)
        notify.assert_not_called()

    def test_only_site_downloads_can_be_paused(self) -> None:
        other = self.ctx.jobs._create(self.user, JobType.FETCH_MISSING, {"novel_id": "novel"})
        with self.assertRaises(Exception):
            self.ctx.jobs.pause(other.id)

    def test_the_list_shows_active_first_without_bulky_details(self) -> None:
        finished = self.export_job("b.com", urls=["https://b.com/1"] * 3000)
        with self.ctx.db.session() as sess:
            self.ctx.jobs._success(sess, finished.id)
            sess.commit()
        running = self.export_job("a.com", urls=["https://a.com/1", "https://a.com/2"])
        self.ctx.jobs._create(self.user, JobType.SEARCH_SOURCE, {"query": "x", "domain": "a.com"})
        rows = self.ctx.jobs.list_downloads(self.user)
        self.assertEqual([row["id"] for row in rows], [running.id, finished.id])
        self.assertNotIn("urls", rows[0]["extra"])
        self.assertEqual(rows[0]["extra"]["urls_count"], 2)
        self.assertEqual(rows[1]["extra"]["urls_count"], 3000)
        self.assertEqual(rows[0]["status"], "PENDING")
        self.assertTrue(rows[1]["is_done"])


class DownloadsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = test_novel_repair.NovelRepairTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.ctx = fixture.ctx
        self.user = fixture.user
        self.client = fixture.client
        self.ctx.scheduler = SimpleNamespace(stop_job=lambda job_id: None)

    def test_pause_resume_and_list_over_the_api(self) -> None:
        job = self.ctx.jobs._create(self.user, JobType.EXPORT_SOURCE, {"domain": "a.com"})
        self.assertEqual(self.client.post(f"/api/job/{job.id}/pause").status_code, 200)
        rows = self.client.get("/api/jobs/downloads")
        self.assertEqual(rows.status_code, 200)
        self.assertEqual(rows.json()[0]["status"], "PAUSED")
        self.assertEqual(self.client.post(f"/api/job/{job.id}/resume").status_code, 200)
        self.assertEqual(self.ctx.jobs.get(job.id).status, JobStatus.PENDING)
