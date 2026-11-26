from types import SimpleNamespace
import unittest
from unittest.mock import patch

import sqlmodel as sq
import test_novel_repair

from lncrawl.dao import Job
from lncrawl.enums import JobStatus, JobType
from lncrawl.services.scheduler import scrubber
from lncrawl.utils.time_utils import current_timestamp

HOUR = 3600 * 1000


class JobActivityTests(unittest.TestCase):
    """A long job that keeps working must not be mistaken for a stuck one."""

    def setUp(self) -> None:
        fixture = test_novel_repair.NovelRepairTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.ctx = fixture.ctx
        self.user = fixture.user
        fixture.stack.enter_context(patch.object(scrubber, "ctx", self.ctx))
        fixture.stack.enter_context(patch.object(scrubber, "_last_look", 0))
        self.stopped: list = []
        self.ctx.scheduler = SimpleNamespace(stop_job=self.stopped.append)

    def running_job(self, age_hours: float) -> Job:
        self.made = (
            getattr(self, "made", 0) + 1
        )  # each its own: the same one asked twice is one job
        job = self.ctx.jobs._create(
            self.user, JobType.EXPORT_SOURCE, {"domain": "novelfire.net", "n": self.made}
        )
        old = current_timestamp() - int(age_hours * HOUR)
        with self.ctx.db.session() as sess:
            sess.exec(
                sq.update(Job)
                .where(sq.col(Job.id) == job.id)
                .values(status=JobStatus.RUNNING, created_at=old, updated_at=old)
            )
            sess.commit()
        return job

    def updated_at(self, job: Job) -> int:
        with self.ctx.db.session() as sess:
            return int(sess.exec(sq.select(Job.updated_at).where(Job.id == job.id)).one())

    def status(self, job: Job) -> JobStatus:
        return self.ctx.jobs.get(job.id).status

    def test_every_kind_of_job_change_counts_as_activity(self) -> None:
        job = self.running_job(20)
        before = self.updated_at(job)
        self.ctx.jobs.update_extra(job, {"phase": "downloading"})
        self.assertGreater(self.updated_at(job), before)

        job = self.running_job(20)
        before = self.updated_at(job)
        with self.ctx.db.session() as sess:
            self.ctx.jobs._update(sess, job.id, done=1, total=50)
            sess.commit()
        self.assertGreater(self.updated_at(job), before)

    def test_a_long_job_still_working_is_left_alone(self) -> None:
        job = self.running_job(20)  # started 20 hours ago
        self.ctx.jobs.update_extra(job, {"current_title": "Blue Exorcist"})  # still going
        scrubber.Scrubber().cancel_long_jobs()
        self.assertEqual(self.status(job), JobStatus.RUNNING)

    def test_a_stuck_job_is_cancelled_by_the_app_not_an_admin(self) -> None:
        job = self.running_job(17)  # nothing for 17 hours
        scrubber.Scrubber().cancel_long_jobs()
        cancelled = self.ctx.jobs.get(job.id)
        self.assertEqual(cancelled.status, JobStatus.CANCELED)
        self.assertIn("no progress for 16 hours", cancelled.error or "")
        self.assertNotIn("admin", cancelled.error or "")
        self.assertEqual(self.stopped, [job.id])  # its thread is told, not only the database

    def test_a_computer_that_slept_does_not_get_its_jobs_cancelled_on_waking(self) -> None:
        job = self.running_job(17)
        # the scrubber itself last looked 9 hours ago: the machine was asleep
        with patch.object(scrubber, "_last_look", current_timestamp() - 9 * HOUR):
            scrubber.Scrubber().cancel_long_jobs()
        self.assertEqual(self.status(job), JobStatus.RUNNING)
        self.assertEqual(self.stopped, [])

    def test_asking_for_the_same_download_twice_gives_one_job(self) -> None:
        first = self.ctx.jobs._create(self.user, JobType.NOVEL, {"url": "https://a.test/n/1"})
        again = self.ctx.jobs._create(self.user, JobType.NOVEL, {"url": "https://a.test/n/1"})
        other = self.ctx.jobs._create(self.user, JobType.NOVEL, {"url": "https://a.test/n/2"})
        self.assertEqual(again.id, first.id)
        self.assertNotEqual(other.id, first.id)
        with self.ctx.db.session() as sess:
            sess.exec(sq.update(Job).where(sq.col(Job.id) == first.id).values(is_done=True))
            sess.commit()
        later = self.ctx.jobs._create(self.user, JobType.NOVEL, {"url": "https://a.test/n/1"})
        self.assertNotEqual(later.id, first.id)  # once it is over, asking again is a new one

    def test_the_downloads_page_says_what_a_download_still_waits_for(self) -> None:
        jobs = self.ctx.jobs
        other = jobs._create(
            self.user, JobType.FETCH_MISSING, {"novel_id": "x", "novel_title": "X", "n": 1}
        )
        older_book = jobs._create(
            self.user, JobType.ARTIFACT, {"novel_id": "x"}, parent_id=other.id
        )
        repair = jobs._create(
            self.user, JobType.FETCH_MISSING, {"novel_id": "y", "novel_title": "Y", "n": 2}
        )
        batch = jobs._create(
            self.user, JobType.CHAPTER_BATCH, {"novel_id": "y"}, parent_id=repair.id
        )
        chapter = jobs._create(self.user, JobType.CHAPTER, {"chapter_id": "c"}, parent_id=batch.id)
        jobs._create(
            self.user, JobType.ARTIFACT, {"novel_id": "y"}, parent_id=repair.id, depends_on=batch.id
        )
        with self.ctx.db.session() as sess:
            sess.exec(
                sq.update(Job)
                .where(sq.col(Job.id).in_([batch.id, repair.id]))
                .values(status=JobStatus.RUNNING)
            )
            sess.exec(sq.update(Job).where(sq.col(Job.id) == chapter.id).values(is_done=True))
            sess.commit()

        def left(job_id: str) -> list:
            rows = {row["id"]: row for row in jobs.list_downloads(self.user)}
            return sorted((x["what"], x["state"]) for x in rows[job_id]["extra"]["left"])

        self.assertEqual(
            left(repair.id),
            [("chapter download", "running"), ("e-book", "waiting for the chapters")],
        )
        with self.ctx.db.session() as sess:
            sess.exec(sq.update(Job).where(sq.col(Job.id) == batch.id).values(is_done=True))
            sess.commit()
        self.assertEqual(left(repair.id), [("e-book", "queued, 1 ahead")])  # behind older_book
        self.assertTrue(older_book.id)


if __name__ == "__main__":
    unittest.main()
