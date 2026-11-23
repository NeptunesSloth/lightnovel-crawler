"""A chapter list from the site never takes downloaded chapters with it."""

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Iterator, List
import unittest
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine, select

from lncrawl.core import Chapter as CrawlerChapter
from lncrawl.dao import Chapter, Novel
from lncrawl.services import chapters as chapters_module
from lncrawl.services.chapters import ChapterService


class SyncKeepsDownloadsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)

        @contextmanager
        def session() -> Iterator[Session]:
            with Session(self.engine) as sess:
                yield sess

        fake = SimpleNamespace(
            db=SimpleNamespace(session=session),
            files=SimpleNamespace(resolve=lambda path: root / path),
        )
        patcher = patch.object(chapters_module, "ctx", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.files = []
        with Session(self.engine) as sess:
            sess.add(Novel(id="n", domain="a.com", url="https://a.com/n", title="N"))
            for serial, done in ((1, True), (2, True), (3, False)):
                sess.add(
                    Chapter(
                        novel_id="n",
                        serial=serial,
                        title=f"C{serial}",
                        url=f"https://a.com/n/{serial}",
                        is_done=done,
                    )
                )
                file = root / f"novels/n/chapters/{serial:06}.zst"
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_bytes(b"text")
                self.files.append(file)
            sess.commit()
        self.service = ChapterService.__new__(ChapterService)

    def serials(self) -> List[int]:
        with Session(self.engine) as sess:
            return sorted(sess.exec(select(Chapter.serial)).all())

    def test_an_empty_list_removes_nothing(self) -> None:
        self.service.sync("n", [])
        self.assertEqual(self.serials(), [1, 2, 3])
        self.assertTrue(all(file.is_file() for file in self.files))

    def test_a_list_cut_short_keeps_the_downloaded_chapters(self) -> None:
        self.service.sync("n", [CrawlerChapter(id=1, title="C1", url="https://a.com/n/1")])
        # 2 is downloaded and stays; 3 never was, and goes with the site's list
        self.assertEqual(self.serials(), [1, 2])
        self.assertTrue(self.files[1].is_file())


if __name__ == "__main__":
    unittest.main()
