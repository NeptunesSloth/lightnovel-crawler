from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import test_novel_repair

from lncrawl.dao import Artifact, ChapterImage
from lncrawl.enums import OutputFormat
from lncrawl.services import page_rescue

FIRST = "0" * 31 + "1"
SECOND = "0" * 31 + "2"


class PageRescueTests(unittest.TestCase):
    """A manga page the library lost comes back out of the novel's own e-book."""

    def setUp(self) -> None:
        fixture = test_novel_repair.NovelRepairTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.ctx = fixture.ctx
        fixture.stack.enter_context(patch.object(page_rescue, "ctx", self.ctx))
        page_rescue._books.clear()
        page_rescue._hint.clear()
        page_rescue._given_up.clear()
        page_rescue._relearned.clear()
        body = "".join(f'<p><img src="images/{i}.jpg" alt="{i}"/></p>' for i in (FIRST, SECOND))
        self.ctx.files.save_text("novels/novel/chapters/000001.zst", body)
        book = self.ctx.files.resolve("novels/novel/artifacts/Test Novel.cbz")
        book.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(book, "w") as archive:  # as made before pages said their ids
            archive.writestr("00001_0001.jpg", b"page one")
            archive.writestr("00001_0002.jpg", b"page two")
            archive.writestr("00002_0001.jpg", b"another chapter")
        with self.ctx.db.session() as sess:
            sess.add(
                Artifact(novel_id="novel", format=OutputFormat.cbz, file_name="Test Novel.cbz")
            )
            # left behind by a novel that was deleted: the page is filed, but nowhere
            sess.add(ChapterImage(id=SECOND, url="x", chapter_id="gone", novel_id="gone"))
            sess.commit()

    def test_a_missing_page_is_taken_from_the_e_book_and_filed(self) -> None:
        for image_id, content in ((FIRST, b"page one"), (SECOND, b"page two")):
            path = page_rescue.restore(image_id, "chapter-1")
            assert path is not None
            self.assertEqual(path.read_bytes(), content)
            image = self.ctx.images.get(image_id)
            self.assertEqual((image.novel_id, image.chapter_id), ("novel", "chapter-1"))

    def test_a_page_of_another_chapter_is_not_filed_on_a_guess(self) -> None:
        self.assertIsNone(page_rescue.restore("f" * 32, "chapter-1"))

    def test_an_e_book_with_a_different_number_of_pages_is_not_trusted(self) -> None:
        self.ctx.files.save_text(
            "novels/novel/chapters/000001.zst", f'<img src="images/{FIRST}.jpg"/>'
        )
        self.assertIsNone(page_rescue.restore(FIRST, "chapter-1"))

    def test_a_page_filed_under_another_novel_folder_is_copied_back(self) -> None:
        self.ctx.files.resolve("novels/novel/artifacts/Test Novel.cbz").unlink()
        other = self.ctx.files.resolve(f"novels/other/images/{FIRST}.jpg")
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_bytes(b"kept elsewhere")
        with self.ctx.db.session() as sess:  # filed under that novel, as an import does
            sess.add(ChapterImage(id=FIRST, url="x", chapter_id="o-1", novel_id="other"))
            sess.commit()
        path = page_rescue.restore(FIRST, "chapter-1")
        assert path is not None
        self.assertEqual(path.read_bytes(), b"kept elsewhere")
        self.assertIn("novels/novel/images", path.as_posix())

    def test_a_downloaded_page_is_fetched_again_from_its_site(self) -> None:
        self.ctx.files.resolve("novels/novel/artifacts/Test Novel.cbz").unlink()
        with self.ctx.db.session() as sess:
            image = sess.get(ChapterImage, SECOND)
            assert image is not None
            image.url = "https://site.test/page-2.jpg"
            sess.add(image)
            sess.commit()
        asked = []

        def fetch(user_id, image_id, signal, refresh=False):
            asked.append((user_id, image_id))
            image = self.ctx.images.get(image_id)
            file = self.ctx.files.resolve(image.image_file)
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(b"from the site")
            return image

        with patch.object(self.ctx.crawler, "fetch_image", fetch):
            path = page_rescue.restore(SECOND, "chapter-1", "user")
            self.assertIsNone(page_rescue.restore(FIRST, "chapter-1", "user"))  # no address
        assert path is not None
        self.assertEqual(path.read_bytes(), b"from the site")
        self.assertEqual(asked, [("user", SECOND)])

    def test_a_chapter_that_cannot_be_had_is_not_looked_for_page_by_page(self) -> None:
        self.ctx.files.resolve("novels/novel/artifacts/Test Novel.cbz").unlink()
        looks = []
        real = page_rescue._found_elsewhere

        def counted(image_id: str, novel_id: str):
            looks.append(image_id)
            return real(image_id, novel_id)

        with patch.object(page_rescue, "_found_elsewhere", counted):
            self.assertIsNone(page_rescue.restore(FIRST, "chapter-1", "user"))
            self.assertIsNone(page_rescue.restore(SECOND, "chapter-1", "user"))
        self.assertEqual(looks, [FIRST])  # the second page was not searched for again

    def test_pages_whose_records_are_gone_are_learned_again_from_the_chapter(self) -> None:
        self.ctx.files.resolve("novels/novel/artifacts/Test Novel.cbz").unlink()
        with self.ctx.db.session() as sess:  # no record of either page is left
            for image in sess.exec(page_rescue.sq.select(ChapterImage)).all():
                sess.delete(image)
            sess.commit()
        read = []

        def download_chapter(model) -> None:
            read.append(model.url)
            model.images = {FIRST: "https://cdn.test/1.png", SECOND: "https://cdn.test/2.png"}

        crawler = SimpleNamespace(download_chapter=download_chapter, format_chapter=lambda m: None)

        @contextmanager
        def prepare_crawler(*args, **kwargs):
            yield crawler

        def fetch(user_id, image_id, signal, refresh=False):
            image = self.ctx.images.get(image_id)
            file = self.ctx.files.resolve(image.image_file)
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(image.url.encode())
            return image

        with (
            patch.object(self.ctx.crawler, "prepare_crawler", prepare_crawler),
            patch.object(self.ctx.crawler, "fetch_image", fetch),
        ):
            first = page_rescue.restore(FIRST, "chapter-1", "user")
            second = page_rescue.restore(SECOND, "chapter-1", "user")
        assert first is not None and second is not None
        self.assertEqual(first.read_bytes(), b"https://cdn.test/1.png")
        self.assertEqual(second.read_bytes(), b"https://cdn.test/2.png")
        self.assertEqual(len(read), 1)  # the chapter was read from its site once


if __name__ == "__main__":
    unittest.main()
