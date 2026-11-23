"""E-books made for the novels without one, before the library ZIP."""

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Iterator, List
import unittest
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine

from lncrawl.dao import Artifact, Chapter, Novel
from lncrawl.enums import OutputFormat
from lncrawl.services import library_books


class LibraryBooksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(self.engine)
        self.made: List[tuple] = []
        self.files = {"novels/old-book/artifacts/Old.epub", "novels/fresh/artifacts/Fresh.epub"}

        @contextmanager
        def session() -> Iterator[Session]:
            with Session(self.engine) as sess:
                yield sess

        def make_artifact(novel_id, title, format, signal=None):
            self.made.append((novel_id, format))
            return f"made {novel_id}"

        fake = SimpleNamespace(
            db=SimpleNamespace(session=session),
            files=SimpleNamespace(exists=lambda path: path in self.files),
            binder=SimpleNamespace(make_artifact=make_artifact),
        )
        patcher = patch.object(library_books, "ctx", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        with Session(self.engine) as sess:
            # (id, chapters, downloaded, when the newest changed, e-book made when, manga)
            for novel_id, listed, done, changed, book, manga in (
                ("none", 3, 3, 100, None, False),  # a library-only download
                ("manga", 2, 2, 100, None, True),
                ("partial", 3, 1, 100, None, False),
                ("details", 0, 0, 0, None, False),
                ("fresh", 3, 3, 100, 200, False),  # its e-book has every chapter
                ("old-book", 3, 3, 300, 200, False),  # chapters came after its e-book
                ("gone", 3, 3, 100, 200, False),  # an e-book whose file is gone
            ):
                sess.add(
                    Novel(
                        id=novel_id,
                        domain="a.com",
                        url=f"https://a.com/{novel_id}",
                        title=novel_id.title(),
                        manga=manga,
                    )
                )
                for serial in range(listed):
                    sess.add(
                        Chapter(
                            novel_id=novel_id,
                            serial=serial + 1,
                            title=f"C{serial}",
                            url=f"https://a.com/{novel_id}/{serial}",
                            is_done=serial < done,
                            updated_at=changed,
                        )
                    )
                if book:
                    name = {"fresh": "Fresh", "old-book": "Old", "gone": "Gone"}[novel_id]
                    sess.add(
                        Artifact(
                            novel_id=novel_id,
                            format=OutputFormat.epub,
                            file_name=f"{name}.epub",
                            created_at=book,
                        )
                    )
            sess.commit()

    def test_downloaded_novels_without_a_whole_e_book_get_one(self) -> None:
        todo = sorted(item["id"] for item in library_books.novels_needing_books(False))
        self.assertEqual(todo, ["gone", "manga", "none", "old-book", "partial"])

    def test_fully_downloaded_only_leaves_out_the_partial_ones(self) -> None:
        todo = sorted(item["id"] for item in library_books.novels_needing_books(True))
        self.assertEqual(todo, ["gone", "manga", "none", "old-book"])

    def test_manga_get_a_cbz_and_how_far_it_is_is_told(self) -> None:
        heard: list = []
        made, failed = library_books.make_missing_books(
            True, lambda phase, done, total, **more: heard.append((phase, done, total))
        )
        self.assertEqual((made, failed), (4, 0))
        self.assertIn(("manga", OutputFormat.cbz), self.made)
        self.assertIn(("none", OutputFormat.epub), self.made)
        self.assertEqual(heard[0], ("making", 0, 4))
        self.assertEqual(heard[-1], ("making", 4, 4))

    def test_a_novel_for_the_phone_gets_its_e_book_made_only_when_needed(self) -> None:
        self.assertEqual(library_books.book_for("none"), "made none")
        fresh = library_books.book_for("fresh")
        self.assertEqual(fresh.file_name, "Fresh.epub")  # every chapter is in it already
        self.assertEqual(self.made, [("none", OutputFormat.epub)])
        self.assertEqual(library_books.book_for("old-book"), "made old-book")
        with self.assertRaisesRegex(ValueError, "No chapters"):
            library_books.book_for("details")


if __name__ == "__main__":
    unittest.main()
