from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from lncrawl.services.notifications import JobNotificationService


class JobNotifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = SimpleNamespace(id="root", parent_job_id=None, is_done=False, extra={})
        self.ctx = SimpleNamespace(
            jobs=SimpleNamespace(get_root=MagicMock(return_value=self.root)),
            logger=SimpleNamespace(warn=MagicMock()),
        )
        patcher = patch("lncrawl.services.notifications.ctx", self.ctx)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service = JobNotificationService.__new__(JobNotificationService)
        self.service._taskman = SimpleNamespace(submit_task=MagicMock())

    def user(self, alerts: dict) -> SimpleNamespace:
        return SimpleNamespace(is_verified=True, extra={"email_alerts": alerts})

    def child(self, is_done: bool) -> SimpleNamespace:
        return SimpleNamespace(id="child", parent_job_id="root", is_done=is_done, extra={})

    def test_child_in_progress_does_not_look_up_its_root(self) -> None:
        self.service.notify(self.user({"20": True}), self.child(is_done=False))
        self.ctx.jobs.get_root.assert_not_called()
        self.service._taskman.submit_task.assert_not_called()

    def test_finished_child_reports_on_its_root(self) -> None:
        self.service.notify(self.user({"20": True}), self.child(is_done=True))
        self.ctx.jobs.get_root.assert_called_once_with("child")
        self.service._taskman.submit_task.assert_called_once()
        self.assertIs(self.service._taskman.submit_task.call_args.args[3], self.root)

    def test_user_without_alerts_costs_no_lookup(self) -> None:
        self.service.notify(self.user({"20": False}), self.child(is_done=True))
        self.service.notify(self.user({}), self.child(is_done=True))
        self.ctx.jobs.get_root.assert_not_called()


if __name__ == "__main__":
    unittest.main()
