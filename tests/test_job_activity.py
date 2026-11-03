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

    def running_job(self, age_hours: float) -> Job:
        job = self.ctx.jobs._create(self.user, JobType.EXPORT_SOURCE, {"domain": "novelfire.net"})
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


if __name__ == "__main__":
    unittest.main()
