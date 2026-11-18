"""Which chapters are saved, without reading every chapter file on every run."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lncrawl.services import file as file_module
from lncrawl.services.file import CHECKED_FILE, FileService
from lncrawl.utils.text_tools import text_compress


class ChapterChecksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        ctx = SimpleNamespace(config=SimpleNamespace(app=SimpleNamespace(app_dir=self.root)))
        patcher = patch.object(file_module, "ctx", ctx)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.files = FileService()
        self.folder = self.root / "novels" / "n" / "chapters"
        self.folder.mkdir(parents=True)
        for serial in (1, 2, 3):
            self.save(serial, f"<p>Chapter {serial} of the story.</p>")
        self.save(4, "<div></div>")  # a page the site sent without the chapter

    def save(self, serial: int, html: str) -> None:
        (self.folder / f"{serial:06}.zst").write_bytes(text_compress(html.encode()))

    def check(self) -> tuple:
        read = []
        real = FileService.has_chapter_content

        def counted(files, path):
            read.append(Path(path).name)
            return real(files, path)

        with patch.object(FileService, "has_chapter_content", counted):
            found = self.files.chapters_with_content("n", [1, 2, 3, 4, 5])
        return found, sorted(read)

    def test_the_saved_chapters_are_found_and_read_only_the_first_time(self) -> None:
        found, read = self.check()
        self.assertEqual(found, {1, 2, 3})  # 4 is empty, 5 was never saved
        self.assertEqual(read, ["000001.zst", "000002.zst", "000003.zst", "000004.zst"])
        self.assertTrue((self.folder / CHECKED_FILE).is_file())
        found, read = self.check()  # the next run
        self.assertEqual(found, {1, 2, 3})
        self.assertEqual(read, ["000004.zst"])  # only the one that was not a chapter

    def test_a_chapter_saved_again_is_read_again(self) -> None:
        self.check()
        self.save(2, "<div></div>")  # downloaded again, and this time empty
        stamp = (self.folder / "000002.zst").stat()
        os.utime(self.folder / "000002.zst", ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 10**9))
        self.save(4, "<p>Now it came through.</p>")
        stamp = (self.folder / "000004.zst").stat()
        os.utime(self.folder / "000004.zst", ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 10**9))
        found, read = self.check()
        self.assertEqual(found, {1, 3, 4})
        self.assertEqual(read, ["000002.zst", "000004.zst"])

    def test_a_deleted_chapter_is_not_counted(self) -> None:
        self.check()
        (self.folder / "000003.zst").unlink()
        found, _ = self.check()
        self.assertEqual(found, {1, 2})

    def test_a_novel_with_no_chapters_folder_has_none(self) -> None:
        self.assertEqual(self.files.chapters_with_content("missing", [1, 2]), set())

    def test_a_damaged_record_of_checks_is_made_again(self) -> None:
        (self.folder / CHECKED_FILE).write_text("not json")
        found, read = self.check()
        self.assertEqual(found, {1, 2, 3})
        self.assertEqual(len(read), 4)


if __name__ == "__main__":
    unittest.main()
