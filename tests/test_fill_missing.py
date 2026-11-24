"""Filling a novel's missing chapters without another copy of it in the library."""

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
from typing import Dict, Iterator, List
import unittest
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine, select

from lncrawl.dao import Chapter, Novel
from lncrawl.services.scheduler.handlers import heal_novel
from lncrawl.services.scheduler.handlers.heal_novel import HealNovelHandler

BODY = "<p>" + "A real paragraph of the chapter. " * 20 + "</p>"


class FillMissingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        with Session(self.engine) as sess:
            sess.add(Novel(id="n", domain="a.com", url="https://a.com/n", title="The Novel"))
            for serial in (1, 2, 3):
                sess.add(
                    Chapter(
                        id=f"c{serial}",
                        novel_id="n",
                        serial=serial,
                        title=f"Chapter {serial}",
                        url=f"https://a.com/n/{serial}",
                        is_done=serial == 1,
                    )
                )
            sess.commit()
        self.saved: Dict[str, str] = {"novels/n/chapters/000001.zst": BODY}
        self.own_site: List[str] = []  # chapter ids the novel's own site gives
        self.asked: List[str] = []
        self.added_novels: List[str] = []

        @contextmanager
        def session() -> Iterator[Session]:
            with Session(self.engine) as sess:
                yield sess

        def chapters_list(novel_id: str) -> List[Chapter]:
            with Session(self.engine) as sess:
                return list(sess.exec(select(Chapter).where(Chapter.novel_id == novel_id)))

        def with_content(novel_id, serials):
            return {s for s in serials if f"novels/{novel_id}/chapters/{s:06}.zst" in self.saved}

        def fetch_chapter(user_id, chapter_id, signal=None, custom=None):
            self.asked.append(chapter_id)
            if chapter_id not in self.own_site:
                raise RuntimeError("404")
            serial = int(chapter_id[1:])
            self.saved[f"novels/n/chapters/{serial:06}.zst"] = BODY

        def fetch_novel(*args, **kwargs):
            self.added_novels.append(args[1])
            raise AssertionError("nothing else may be added to the library")

        other = SimpleNamespace(
            read_novel=lambda model: setattr(
                model,
                "chapters",
                [
                    SimpleNamespace(title="Chapter 2", url="https://b.com/x/2"),
                    SimpleNamespace(title="Chapter 3", url="https://b.com/x/3"),
                    SimpleNamespace(title="Chapter 9", url="https://b.com/x/9"),
                ],
            ),
            format_novel=lambda model: None,
            download_chapter=lambda model: setattr(model, "body", BODY),
            format_chapter=lambda model: None,
            close=lambda: None,
        )

        @contextmanager
        def prepare_crawler(user_id, url, signal=None, custom=None, pooled=False):
            yield other

        self.ctx = SimpleNamespace(
            db=SimpleNamespace(session=session),
            novels=SimpleNamespace(
                get=lambda novel_id: SimpleNamespace(
                    id="n", title="The Novel", domain="a.com", url="https://a.com/n"
                ),
                heal_from_library=lambda novel_id: {"healed": 0},
            ),
            chapters=SimpleNamespace(list=lambda novel_id: chapters_list(novel_id)),
            files=SimpleNamespace(
                chapters_with_content=with_content,
                save_text=lambda path, body: self.saved.__setitem__(path, body),
            ),
            sources=SimpleNamespace(
                init_crawler=lambda url: SimpleNamespace(close=lambda: None),
                list=lambda can_search: [
                    SimpleNamespace(domain="a.com"),
                    SimpleNamespace(domain="b.com"),
                ],
            ),
            crawler=SimpleNamespace(
                fetch_chapter=fetch_chapter,
                fetch_novel=fetch_novel,
                prepare_crawler=prepare_crawler,
                search_novel=lambda user_id, query, domain, signal: [
                    SimpleNamespace(title="The Novel", url="https://b.com/x")
                ],
            ),
            logger=SimpleNamespace(debug=lambda *a: None, info=lambda *a: None),
        )
        patcher = patch.object(heal_novel, "ctx", self.ctx)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_job(self) -> Dict:
        handler = HealNovelHandler.__new__(HealNovelHandler)
        handler.job = SimpleNamespace(id="job", extra={"novel_id": "n"}, done=0, total=0)
        handler.user = SimpleNamespace(id="u")
        handler.signal = Event()
        said: Dict = {}
        handler._set_running = lambda: None
        handler._set_progress = lambda done, total: None
        handler._set_extra = lambda **values: said.update(values)
        handler.run()
        return said

    def done(self) -> List[int]:
        with Session(self.engine) as sess:
            return sorted(c.serial for c in sess.exec(select(Chapter)) if c.is_done)

    def test_the_novels_own_site_is_asked_first_for_just_the_gaps(self) -> None:
        self.own_site = ["c2", "c3"]
        said = self.run_job()
        self.assertEqual(self.asked, ["c2", "c3"])
        self.assertEqual(said["healed"], 2)
        self.assertIn("Filled all 2", said["message"])

    def test_what_its_site_will_not_give_comes_from_another_site_with_no_copy_added(self) -> None:
        self.own_site = ["c2"]
        said = self.run_job()
        self.assertEqual(said["healed"], 2)
        self.assertEqual(self.saved["novels/n/chapters/000003.zst"], BODY)
        self.assertEqual(self.done(), [1, 3])  # 2 is marked by the chapter fetch itself
        self.assertEqual(self.added_novels, [])
        with Session(self.engine) as sess:
            self.assertEqual(sess.get(Chapter, "c3").extra["filled_from"], "https://b.com/x/3")
            self.assertEqual(len(sess.exec(select(Novel)).all()), 1)

    def test_it_says_what_could_not_be_found(self) -> None:
        self.ctx.crawler.search_novel = lambda user_id, query, domain, signal: []
        said = self.run_job()
        self.assertEqual(said["healed"], 0)
        self.assertIn("no other searchable site", said["message"])


if __name__ == "__main__":
    unittest.main()
