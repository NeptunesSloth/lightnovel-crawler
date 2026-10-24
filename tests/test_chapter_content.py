from contextlib import ExitStack, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from zipfile import ZipFile

from lncrawl.core import Chapter as CrawlerChapter, Crawler
from lncrawl.dao import Artifact, Chapter, Novel, OutputFormat, Volume
from lncrawl.exceptions import AbortedException, EmptyChapterError
from lncrawl.services.binder.epub import make_epub
from lncrawl.services.crawler import CrawlerService
from lncrawl.services.file import FileService
from lncrawl.utils.chapter_content import NOVELFIRE_LOGIN_NOTICE, has_chapter_content


class ChapterContentTests(unittest.TestCase):
    def test_novelfire_login_notice_is_not_story_content(self) -> None:
        for body in (
            f"<p>{NOVELFIRE_LOGIN_NOTICE}</p>",
            f"<h2>Chapter 8 - 8: Facing The Golden Duke</h2><p>{NOVELFIRE_LOGIN_NOTICE}</p>",
            "<p>Please login to use features: save the novel you are reading,<br>"
            "save the reading position and note your favorite content.</p>",
        ):
            with self.subTest(body=body):
                self.assertFalse(has_chapter_content(body))

    def test_login_notice_does_not_invalidate_real_story_text(self) -> None:
        self.assertTrue(
            has_chapter_content(f"<p>The duke entered the room.</p><p>{NOVELFIRE_LOGIN_NOTICE}</p>")
        )

    def test_empty_markup_is_not_content(self) -> None:
        for body in (
            "",
            '<img src="">',
            '<img src="   ">',
            " \n",
            "<div><p>&nbsp;\u200b</p><br></div>",
            "<!-- text --><script>text</script><style>text</style><template>text</template>",
        ):
            with self.subTest(body=body):
                self.assertFalse(has_chapter_content(body))

    def test_text_and_image_chapters_are_content(self) -> None:
        for body in ("A short chapter.", "<p>正文</p>", '<p><img src="page.jpg"></p>'):
            with self.subTest(body=body):
                self.assertTrue(has_chapter_content(body))

    def test_formatter_rejects_empty_markup(self) -> None:
        crawler = SimpleNamespace(version=1, _extract_images=MagicMock())
        chapter = CrawlerChapter(id=1, title="Chapter 1", url="https://example.com/1")
        chapter.body = "<p>&nbsp;</p>"
        with self.assertRaises(EmptyChapterError):
            Crawler.format_chapter(crawler, chapter)
        self.assertFalse(chapter.success)
        crawler._extract_images.assert_not_called()


class ChapterDownloadTests(unittest.TestCase):
    def test_login_notice_response_is_retried(self) -> None:
        self.responses(f"<p>{NOVELFIRE_LOGIN_NOTICE}</p>", "<p>The actual chapter text.</p>")
        self.fetch()
        self.assertEqual(self.crawler.download_chapter.call_count, 2)
        self.assertEqual(
            self.files.load_text(self.chapter.content_file), "<p>The actual chapter text.</p>"
        )

    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(TemporaryDirectory()))
        self.chapter = Chapter(
            novel_id="novel",
            serial=1,
            title="Chapter 1",
            url="https://example.com/1",
            extra={"crawler_version": 1},
        )
        self.files = FileService()
        self.ctx = SimpleNamespace(
            config=SimpleNamespace(app=SimpleNamespace(app_dir=root)),
            files=self.files,
            chapters=SimpleNamespace(get=MagicMock(return_value=self.chapter)),
            novels=SimpleNamespace(
                get=MagicMock(
                    return_value=SimpleNamespace(
                        url="https://example.com/novel",
                        title="Novel",
                    )
                )
            ),
            images=SimpleNamespace(sync=MagicMock()),
            db=SimpleNamespace(session=MagicMock()),
        )
        for module in ("lncrawl.services.crawler", "lncrawl.dao.chapter", "lncrawl.services.file"):
            self.stack.enter_context(patch(f"{module}.ctx", self.ctx))
        self.crawler = SimpleNamespace(
            version=1,
            scraper=SimpleNamespace(signal=MagicMock()),
            download_chapter=MagicMock(),
            format_chapter=MagicMock(),  # A plugin overriding the base formatter.
        )
        self.crawler.scraper.signal.is_set.return_value = False
        self.crawler.scraper.signal.wait.return_value = False
        self.service = CrawlerService()

        @contextmanager
        def prepare(*args, **kwargs):
            yield self.crawler

        self.stack.enter_context(patch.object(self.service, "prepare_crawler", prepare))

    def responses(self, *bodies: str) -> None:
        remaining = iter(bodies)

        def download(chapter: CrawlerChapter) -> None:
            chapter.body = next(remaining)

        self.crawler.download_chapter.side_effect = download

    def fetch(self, refresh: bool = False) -> Chapter:
        return self.service.fetch_chapter("user", self.chapter.id, refresh=refresh)

    def test_empty_response_retried_before_persisting(self) -> None:
        self.responses("", "<p>&nbsp;</p>", "<p>Recovered chapter text.</p>")
        self.fetch()
        self.assertTrue(self.chapter.is_done)
        self.assertEqual(
            self.files.load_text(self.chapter.content_file), "<p>Recovered chapter text.</p>"
        )
        self.assertEqual(self.crawler.download_chapter.call_count, 3)
        calls = self.crawler.download_chapter.call_args_list
        self.assertIsNot(calls[0].args[0], calls[1].args[0])
        self.ctx.images.sync.assert_called_once()

    def test_permanently_empty_chapter_is_not_saved(self) -> None:
        self.responses("", "<div></div>", " ")
        with self.assertRaises(EmptyChapterError):
            self.fetch()
        self.assertFalse(self.chapter.is_done)
        self.assertFalse(self.files.exists(self.chapter.content_file))
        self.ctx.images.sync.assert_not_called()
        self.assertEqual(self.chapter.extra["download_status"], "empty")
        self.ctx.db.session.return_value.__enter__.return_value.commit.assert_called_once()

    def test_old_blank_cache_is_repaired(self) -> None:
        self.files.save_text(self.chapter.content_file, "<p>&nbsp;</p>")
        self.chapter.is_done = True
        self.assertFalse(self.chapter.is_available)
        self.responses("<p>Recovered.</p>")
        self.fetch()
        self.assertTrue(self.chapter.is_available)
        self.assertTrue(self.chapter.is_done)
        self.crawler.download_chapter.assert_called_once()

    def test_repaired_text_reaches_epub(self) -> None:
        self.files.save_text(self.chapter.content_file, "<p></p>")
        self.chapter.is_done = True
        self.responses("<p>Previously missing chapter text.</p>")
        self.fetch()
        novel = Novel(id="novel", title="Novel", url="https://example.com/novel")
        volume = Volume(novel_id=novel.id, serial=1, title="Volume 1")
        self.ctx.novels.get.return_value = novel
        self.ctx.volumes = SimpleNamespace(list=MagicMock(return_value=[volume]))
        self.ctx.chapters.list = MagicMock(return_value=[self.chapter])
        self.ctx.images.list = MagicMock(return_value=[])
        artifact = Artifact(novel_id=novel.id, format=OutputFormat.epub, file_name="novel.epub")
        working = self.files.root / "tmp"
        working.mkdir()
        with (
            patch("lncrawl.services.binder.epub.ctx", self.ctx),
            patch("lncrawl.dao.novel.ctx", self.ctx),
        ):
            make_epub(working, artifact)
        with ZipFile(self.files.resolve(artifact.output_file)) as book:
            chapter_files = [
                name for name in book.namelist() if name.endswith("chapter_00001.xhtml")
            ]
            self.assertEqual(len(chapter_files), 1)
            self.assertIn(b"Previously missing chapter text.", book.read(chapter_files[0]))

    def test_failed_repair_clears_stale_completion(self) -> None:
        self.files.save_text(self.chapter.content_file, "")
        self.chapter.is_done = True
        self.responses("", "", "")
        with self.assertRaises(EmptyChapterError):
            self.fetch()
        self.assertFalse(self.chapter.is_done)
        self.assertEqual(self.chapter.extra["download_status"], "empty")
        self.assertIn("No chapter content", self.chapter.extra["download_error"])

    def test_valid_cache_skips_download(self) -> None:
        self.files.save_text(self.chapter.content_file, "<p>Existing text.</p>")
        self.fetch()
        self.crawler.download_chapter.assert_not_called()

    def test_crawler_update_does_not_download_valid_cached_text_again(self) -> None:
        self.files.save_text(self.chapter.content_file, "<p>Previously downloaded text.</p>")
        self.chapter.extra = {"crawler_version": 0}
        self.fetch()
        self.crawler.download_chapter.assert_not_called()

    def test_crawler_update_does_not_download_cached_images_again(self) -> None:
        image = SimpleNamespace(
            id="image",
            novel_id="novel",
            url="https://example.com/image.jpg",
            is_available=True,
            extra={"crawler_version": 0},
        )
        self.ctx.images.get = MagicMock(return_value=image)
        self.assertIs(self.service.fetch_image("user", "image", custom=self.crawler), image)

    def test_explicit_refresh_can_replace_valid_cached_text(self) -> None:
        self.files.save_text(self.chapter.content_file, "<p>Old chapter text.</p>")
        self.responses("<p>Updated chapter text.</p>")
        self.fetch(refresh=True)
        self.crawler.download_chapter.assert_called_once()
        self.assertEqual(
            self.files.load_text(self.chapter.content_file), "<p>Updated chapter text.</p>"
        )

    def test_failed_refresh_preserves_existing_text(self) -> None:
        self.files.save_text(self.chapter.content_file, "<p>Existing text.</p>")
        self.chapter.is_done = True
        self.responses("", "", "")
        with self.assertRaises(EmptyChapterError):
            self.fetch(refresh=True)
        self.assertEqual(self.files.load_text(self.chapter.content_file), "<p>Existing text.</p>")
        self.assertTrue(self.chapter.is_done)

    def test_image_only_chapter_is_saved(self) -> None:
        self.responses('<img src="images/page.jpg">')
        self.fetch()
        self.assertTrue(self.chapter.is_available)

    def test_cancellation_stops_retry(self) -> None:
        self.responses("")
        self.crawler.scraper.signal.wait.return_value = True
        with self.assertRaises(AbortedException):
            self.fetch()
        self.crawler.download_chapter.assert_called_once()
        self.assertFalse(self.files.exists(self.chapter.content_file))

    def test_other_errors_are_not_retried(self) -> None:
        self.crawler.download_chapter.side_effect = ValueError("Bad source selector")
        with self.assertRaisesRegex(ValueError, "Bad source selector"):
            self.fetch()
        self.crawler.download_chapter.assert_called_once()


if __name__ == "__main__":
    unittest.main()
