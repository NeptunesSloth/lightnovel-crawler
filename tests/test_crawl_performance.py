from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from sqlmodel import Session, create_engine

from lncrawl.core.cleaner import TextCleaner
from lncrawl.dao import ChapterImage
from lncrawl.enums import OutputFormat
from lncrawl.services.crawler import CrawlerService
from lncrawl.services.file import FileService
from lncrawl.services.images import ChapterImageService
from lncrawl.services.scheduler.handlers.export_source import _download_novel


class ContentCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(TemporaryDirectory()))
        self.files = FileService()
        self.stack.enter_context(
            patch(
                "lncrawl.services.file.ctx",
                SimpleNamespace(config=SimpleNamespace(app=SimpleNamespace(app_dir=self.root))),
            )
        )

    def test_unchanged_chapter_is_parsed_once(self) -> None:
        from lncrawl.utils.chapter_content import has_chapter_content

        self.files.save_text("chapter.zst", "<p>A real chapter.</p>")
        with patch(
            "lncrawl.services.file.has_chapter_content", wraps=has_chapter_content
        ) as validate:
            for _ in range(10):
                self.assertTrue(self.files.has_chapter_content("chapter.zst"))
            validate.assert_called_once()

    def test_replacement_invalidates_cache_and_repair_is_detected(self) -> None:
        self.files.save_text("chapter.zst", "<p>text</p>")
        self.assertTrue(self.files.has_chapter_content("chapter.zst"))
        self.files.save_text("chapter.zst", "<p></p>")
        self.assertFalse(self.files.has_chapter_content("chapter.zst"))
        self.files.save_text("chapter.zst", "<p>Repaired text.</p>")
        self.assertTrue(self.files.has_chapter_content("chapter.zst"))

    def test_same_size_external_edit_invalidates_cache(self) -> None:
        file = self.root / "chapter.html"
        file.write_text("<p>text</p>")
        self.assertTrue(self.files.has_chapter_content(file))
        file.write_text("<p>    </p>")
        self.assertFalse(self.files.has_chapter_content(file))

    def test_deleted_and_missing_files_are_unavailable(self) -> None:
        self.assertFalse(self.files.has_chapter_content("missing.zst"))
        file = self.files.save_text("chapter.zst", "<p>text</p>")
        self.assertTrue(self.files.has_chapter_content(file))
        file.unlink()
        self.assertFalse(self.files.has_chapter_content(file))
        self.assertFalse(self.files.has_chapter_content(self.root))

    def test_cleaner_customization_is_isolated(self) -> None:
        one, two = TextCleaner(), TextCleaner()
        self.assertEqual(one.clean_text("A\u200bB"), "AB")
        one.invisible_chars.append(ord("Z"))
        one.nonprintable_mapping[ord("Z")] = None
        self.assertNotIn(ord("Z"), two.invisible_chars)
        self.assertEqual(two.clean_text("Z\u200b"), "Z")


class PendingChapterTests(unittest.TestCase):
    def test_cached_bulk_export_uses_no_rate_limited_download_slots(self) -> None:
        ids = [f"chapter-{i}" for i in range(516)]
        chapters = [
            SimpleNamespace(id=chapter_id, extra={"crawler_version": 1}, is_available=True)
            for chapter_id in ids
        ]
        crawler = SimpleNamespace(version=2, taskman=SimpleNamespace(submit_task=MagicMock()))
        service = CrawlerService()
        ctx = SimpleNamespace(
            crawler=service,
            novels=SimpleNamespace(
                find_by_url=MagicMock(
                    return_value=SimpleNamespace(
                        id="novel",
                        title="Novel",
                        synopsis="A synopsis.",
                    )
                )
            ),
            chapters=SimpleNamespace(
                list_ids=MagicMock(return_value=ids),
                get_many=MagicMock(return_value=chapters),
            ),
            images=SimpleNamespace(list_ids=MagicMock(return_value=[])),
            binder=SimpleNamespace(
                depends_on_epub=set(),
                make_artifact=MagicMock(
                    return_value=SimpleNamespace(is_available=True, output_file="novel.epub")
                ),
            ),
            files=SimpleNamespace(resolve=lambda file: Path(file)),
        )
        progress = MagicMock()
        with (
            patch("lncrawl.services.crawler.ctx", ctx),
            patch(
                "lncrawl.services.scheduler.handlers.export_source.ctx",
                ctx,
            ),
        ):
            result = _download_novel(
                crawler,
                "user",
                "https://example.com/novel",
                OutputFormat.epub,
                Event(),
                on_novel=progress,
            )
        self.assertEqual(result["missing"], 0)
        self.assertEqual(result["file"], Path("novel.epub"))
        crawler.taskman.submit_task.assert_not_called()
        progress.assert_called_once_with("Novel", 516, 516)

    def test_only_missing_or_empty_chapters_are_queued(self) -> None:
        chapters = [
            SimpleNamespace(id="cached", extra={"crawler_version": 2}, is_available=True),
            SimpleNamespace(id="empty", extra={"crawler_version": 2}, is_available=False),
            SimpleNamespace(id="outdated", extra={"crawler_version": 1}, is_available=True),
        ]
        get_many = MagicMock(return_value=chapters)
        with patch(
            "lncrawl.services.crawler.ctx",
            SimpleNamespace(chapters=SimpleNamespace(get_many=get_many)),
        ):
            pending = CrawlerService().pending_chapter_ids(
                ["cached", "empty", "outdated", "unknown", "cached"],
            )
        self.assertEqual(pending, ["empty", "unknown"])
        get_many.assert_called_once_with(["cached", "empty", "outdated", "unknown"])

    def test_large_selection_uses_bounded_queries(self) -> None:
        get_many = MagicMock(return_value=[])
        ids = [str(i) for i in range(1201)]
        with patch(
            "lncrawl.services.crawler.ctx",
            SimpleNamespace(chapters=SimpleNamespace(get_many=get_many)),
        ):
            self.assertEqual(CrawlerService().pending_chapter_ids(ids), ids)
        self.assertEqual([len(call.args[0]) for call in get_many.call_args_list], [500, 500, 201])


class ImageQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)
        ChapterImage.__table__.create(self.engine)
        with Session(self.engine) as session:
            session.add_all(
                [
                    ChapterImage(
                        id=f"image-{i}",
                        novel_id="novel",
                        chapter_id=f"chapter-{i}",
                        url=f"https://example.com/{i}.jpg",
                    )
                    for i in range(1001)
                ]
            )
            session.add(
                ChapterImage(
                    id="other",
                    novel_id="other-novel",
                    chapter_id="other-chapter",
                    url="https://example.com/other.jpg",
                )
            )
            session.commit()
        self.session_factory = MagicMock(side_effect=lambda: Session(self.engine))
        self.patch = patch(
            "lncrawl.services.images.ctx",
            SimpleNamespace(db=SimpleNamespace(session=self.session_factory)),
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.service = ChapterImageService()

    def test_selected_chapters_exclude_other_images_and_duplicates(self) -> None:
        self.assertCountEqual(
            self.service.list_ids(
                novel_id="novel",
                chapter_ids=["chapter-1", "chapter-2", "other-chapter", "chapter-1"],
            ),
            ["image-1", "image-2"],
        )
        self.assertEqual(self.session_factory.call_count, 1)

    def test_empty_selection_does_not_fetch_whole_library(self) -> None:
        self.assertEqual(self.service.list_ids(chapter_ids=[]), [])
        self.session_factory.assert_not_called()

    def test_pending_image_query_excludes_completed_images(self) -> None:
        with Session(self.engine) as session:
            image = session.get(ChapterImage, "image-1")
            image.is_done = True
            session.add(image)
            session.commit()
        pending = self.service.list(novel_id="novel", is_crawled=False)
        self.assertNotIn("image-1", [image.id for image in pending])
        self.assertEqual(len(pending), 1000)
        self.assertEqual(
            [image.id for image in self.service.list(novel_id="novel", is_crawled=True)],
            ["image-1"],
        )

    def test_large_selection_and_existing_single_chapter_api(self) -> None:
        images = self.service.list_ids(chapter_ids=[f"chapter-{i}" for i in range(1001)])
        self.assertEqual(len(images), 1001)
        self.assertEqual(self.service.list_ids(chapter_id="chapter-2"), ["image-2"])


if __name__ == "__main__":
    unittest.main()
