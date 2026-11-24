"""Deleting the novels that are not fully downloaded, all at once."""

from contextlib import contextmanager
import time
from types import SimpleNamespace
from typing import Iterator, List
import unittest
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine

from lncrawl.dao import Chapter, Job, Novel, User
from lncrawl.enums import JobStatus, JobType
from lncrawl.services import unfinished


class UnfinishedNovelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        self.deleted: List[str] = []

        @contextmanager
        def session() -> Iterator[Session]:
            with Session(self.engine) as sess:
                yield sess

        def delete(novel_id: str) -> bool:
            self.deleted.append(novel_id)
            return True

        fake = SimpleNamespace(
            db=SimpleNamespace(session=session), novels=SimpleNamespace(delete=delete)
        )
        patcher = patch.object(unfinished, "ctx", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        with Session(self.engine) as sess:
            sess.add(User(id="u", email="u@example.com", password="x"))
            # chapters downloaded of the chapters listed
            for novel_id, site, listed, done in (
                ("complete", "a.com", 3, 3),
                ("partial", "a.com", 3, 1),
                ("details", "a.com", 0, 0),
                ("listed", "a.com", 2, 0),
                ("busy-site", "b.com", 2, 1),
                ("busy-novel", "a.com", 2, 0),
            ):
                sess.add(
                    Novel(
                        id=novel_id, domain=site, url=f"https://{site}/{novel_id}", title=novel_id
                    )
                )
                for serial in range(listed):
                    sess.add(
                        Chapter(
                            novel_id=novel_id,
                            serial=serial + 1,
                            url=f"https://{site}/{novel_id}/{serial}",
                            title=f"Chapter {serial + 1}",
                            is_done=serial < done,
                        )
                    )
            sess.add(
                Job(
                    user_id="u",
                    type=JobType.EXPORT_SOURCE,
                    status=JobStatus.RUNNING,
                    extra={"url": "https://www.b.com/"},
                )
            )
            sess.add(
                Job(
                    user_id="u",
                    type=JobType.EXPORT_SOURCE,
                    status=JobStatus.PAUSED,
                    extra={"url": "https://c.com/", "urls": ["https://a.com/busy-novel"]},
                )
            )
            sess.add(  # finished: keeps nothing
                Job(
                    user_id="u",
                    type=JobType.EXPORT_SOURCE,
                    status=JobStatus.SUCCESS,
                    is_done=True,
                    extra={"url": "https://a.com/"},
                )
            )
            sess.commit()

    def test_what_is_not_fully_downloaded_is_found_and_downloads_keep_theirs(self) -> None:
        found = unfinished.find()
        self.assertEqual(
            sorted(item["id"] for item in found["novels"]), ["details", "listed", "partial"]
        )
        self.assertEqual(found["details_only"], 2)
        self.assertEqual(found["partial"], 1)
        self.assertEqual(found["chapters"], 1)
        self.assertEqual(found["kept"], 2)

    def test_a_downloaded_novel_with_new_chapters_since_is_not_unfinished(self) -> None:
        day = 24 * 3600 * 1000
        with Session(self.engine) as sess:
            sess.add(Novel(id="behind", domain="a.com", url="https://a.com/behind", title="b"))
            for serial, done, when in ((1, True, 1000), (2, True, 1000), (3, False, 1000 + day)):
                sess.add(
                    Chapter(
                        novel_id="behind",
                        serial=serial,
                        url=f"https://a.com/behind/{serial}",
                        title=f"Chapter {serial}",
                        is_done=done,
                        created_at=when,
                    )
                )
            sess.commit()
        found = unfinished.find()
        self.assertNotIn("behind", [item["id"] for item in found["novels"]])
        self.assertEqual(found["behind"], 1)
        self.assertEqual(found["partial"], 1)  # the one a download stopped in still counts

    def test_one_kind_can_be_chosen(self) -> None:
        self.assertEqual(
            sorted(item["id"] for item in unfinished.find("details")["novels"]),
            ["details", "listed"],
        )
        self.assertEqual([item["id"] for item in unfinished.find("partial")["novels"]], ["partial"])
        with self.assertRaises(ValueError):
            unfinished.start("everything")

    def wait(self) -> dict:
        for _ in range(200):
            if not unfinished.status()["running"]:
                break
            time.sleep(0.01)
        return unfinished.status()

    def test_only_the_partly_downloaded_ones_go_when_asked(self) -> None:
        unfinished.start("partial")
        state = self.wait()
        self.assertEqual((state["kind"], state["done"], state["total"]), ("partial", 1, 1))
        self.assertEqual(self.deleted, ["partial"])

    def test_removing_deletes_them_in_the_background_and_reports(self) -> None:
        unfinished.start()
        for _ in range(200):
            if not unfinished.status()["running"]:
                break
            time.sleep(0.01)
        state = unfinished.status()
        self.assertFalse(state["running"])
        self.assertEqual((state["done"], state["total"], state["failed"]), (3, 3, 0))
        self.assertEqual(sorted(self.deleted), ["details", "listed", "partial"])


if __name__ == "__main__":
    unittest.main()
