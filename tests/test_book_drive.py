"""A library on this computer with its books on a drive that comes and goes."""

import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lncrawl import relocation
from lncrawl.services import books as books_module
from lncrawl.services.books import BookDrive, BookDriveUnplugged


def make_library(folder: Path) -> None:
    folder.mkdir(parents=True)
    database = sqlite3.connect(str(folder / "sqlite.db"))
    database.execute("CREATE TABLE novels (id TEXT)")
    database.execute("INSERT INTO novels VALUES ('a')")
    database.commit()
    database.close()
    (folder / "config.json").write_text("{}")
    (folder / relocation.STAMP).write_text(f"{relocation.IN_USE}\n")
    for novel in ("a", "b"):
        chapters = folder / "novels" / novel / "chapters"
        chapters.mkdir(parents=True)
        (chapters / "000001.zst").write_bytes(b"chapter")
        (folder / "novels" / novel / "cover.jpg").write_bytes(b"cover")
    (folder / "exports" / "manifest").mkdir(parents=True)
    (folder / "exports" / "manifest" / "site.json").write_text("{}")
    (folder / "exports" / "big.zip").write_bytes(b"zip")
    (folder / "library.zip").write_bytes(b"zip")
    cache = folder / "app-browser" / "Default" / "Cache"
    cache.mkdir(parents=True)
    (cache / "data_1").write_bytes(b"cache")
    (folder / "app-browser" / "Default" / "Preferences").write_text("{}")
    (folder / "webdriver").mkdir()
    (folder / "webdriver" / "profile").write_text("x")


class SplitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.base, self.drive, self.here = root / "base", root / "F" / "Novels", root / "laptop"
        self.base.mkdir()
        make_library(self.drive)
        relocation.point_at(self.base, self.drive)

    def test_the_next_start_keeps_the_library_here_and_the_books_on_the_drive(self) -> None:
        relocation.begin_split(self.base, self.drive, self.here)
        self.assertEqual(relocation.resolve(self.base), self.here)
        # the app's own files and the covers came; the books and big files stayed
        self.assertTrue((self.here / "sqlite.db").is_file())
        self.assertTrue((self.here / "config.json").is_file())
        self.assertTrue((self.here / "novels" / "a" / "cover.jpg").is_file())
        self.assertFalse((self.here / "novels" / "a" / "chapters").exists())
        self.assertTrue((self.here / "exports" / "manifest" / "site.json").is_file())
        self.assertFalse((self.here / "exports" / "big.zip").exists())
        self.assertFalse((self.here / "library.zip").exists())
        # a browser's profile comes, not its cache; the crawler's is made again
        self.assertTrue((self.here / "app-browser" / "Default" / "Preferences").is_file())
        self.assertFalse((self.here / "app-browser" / "Default" / "Cache").exists())
        self.assertFalse((self.here / "webdriver").exists())
        # nothing of the books was removed; the drive is no longer a library
        self.assertTrue((self.drive / "novels" / "a" / "chapters" / "000001.zst").is_file())
        self.assertFalse((self.drive / "sqlite.db").exists())
        self.assertFalse(relocation.is_library(self.drive))
        config = json.loads((self.here / relocation.BOOKS).read_text())
        self.assertEqual(config["folder"], str(self.drive))
        self.assertEqual(config["novels"], ["a", "b"])
        # and the start after that opens here, drive or not
        self.assertEqual(relocation.resolve(self.base), self.here)
        database = sqlite3.connect(str(self.here / "sqlite.db"))
        self.assertEqual(database.execute("SELECT id FROM novels").fetchall(), [("a",)])
        database.close()

    def test_a_split_into_the_drive_itself_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            relocation.begin_split(self.base, self.drive, self.drive / "inside")

    def test_a_split_that_cannot_be_done_leaves_the_library_where_it_was(self) -> None:
        relocation.begin_split(self.base, self.drive, self.here)
        with patch.object(relocation, "_snapshot_database", side_effect=OSError("disk full")):
            self.assertEqual(relocation.resolve(self.base), self.drive)
        self.assertTrue((self.drive / "sqlite.db").is_file())
        self.assertNotIn("split_to", relocation.read_marker(self.base))
        self.assertIn("disk full", relocation.read_marker(self.base)["failed"])


class BookDriveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.base, self.drive, self.here = root / "base", root / "F" / "Novels", root / "laptop"
        self.base.mkdir()
        make_library(self.drive)
        relocation.point_at(self.base, self.drive)
        relocation.begin_split(self.base, self.drive, self.here)
        self.assertEqual(relocation.resolve(self.base), self.here)
        fake = SimpleNamespace(config=SimpleNamespace(app=SimpleNamespace(app_dir=self.here)))
        patcher = patch.object(books_module, "ctx", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.books = BookDrive()

    def unplug(self) -> None:
        (self.drive / relocation.BOOK_STAMP).rename(self.drive.parent / "stamp")
        self.books._checked = -books_module.PLUGGED_CHECK

    def route(self, path: str) -> Path:
        found = self.books.route(Path(path))
        return found if found is not None else self.here / path

    def test_books_are_read_from_the_drive_and_covers_from_here(self) -> None:
        self.assertTrue(self.books.plugged())
        self.assertEqual(
            self.route("novels/a/chapters/000001.zst"),
            self.drive / "novels" / "a" / "chapters" / "000001.zst",
        )
        self.assertEqual(self.route("novels/a/cover.jpg"), self.here / "novels" / "a" / "cover.jpg")
        self.assertEqual(self.route("logs/app.log"), self.here / "logs" / "app.log")

    def test_unplugged_the_books_say_so_and_new_novels_stay_here(self) -> None:
        self.unplug()
        self.assertFalse(self.books.plugged())
        self.assertTrue(self.books.unavailable("a"))
        self.assertEqual(
            self.route("novels/new/chapters/1.zst").parent.parent.parent, self.here / "novels"
        )

    def test_a_new_novel_goes_to_the_drive_when_it_is_plugged_in(self) -> None:
        self.assertEqual(
            self.route("novels/new/chapters/000001.zst"),
            self.drive / "novels" / "new" / "chapters" / "000001.zst",
        )
        saved = json.loads((self.here / relocation.BOOKS).read_text())
        self.assertIn("new", saved["novels"])
        self.books.set_new("here")
        self.assertEqual(
            self.route("novels/other/chapters/000001.zst"),
            self.here / "novels" / "other" / "chapters" / "000001.zst",
        )

    def test_the_drive_is_found_under_another_letter(self) -> None:
        moved = self.drive.parent.parent / "G" / "Novels"
        moved.parent.mkdir()
        self.drive.rename(moved)
        with (
            patch.object(
                books_module, "drive_roots", return_value=[self.drive.parent.parent / "G"]
            ),
            patch.object(books_module.os.path, "splitdrive", return_value=("F:", "/Novels")),
        ):
            self.assertTrue(self.books.plugged())
        self.assertEqual(self.books.folder(), moved)

    def wait_moved(self) -> dict:
        for _ in range(500):
            status = self.books.status()
            if not status["moving"]["running"]:
                return status
            time.sleep(0.01)
        self.fail("still moving")

    def test_novels_move_here_and_back(self) -> None:
        self.books.move(["a"], to_drive=False)
        status = self.wait_moved()
        self.assertEqual(status["on_drive"], ["b"])
        self.assertTrue((self.here / "novels" / "a" / "chapters" / "000001.zst").is_file())
        self.assertFalse((self.drive / "novels" / "a").exists())
        self.unplug()
        self.assertFalse(self.books.unavailable("a"))  # readable without the drive
        (self.drive.parent / "stamp").rename(self.drive / relocation.BOOK_STAMP)
        self.books._checked = -books_module.PLUGGED_CHECK
        self.books.move(["a"], to_drive=True)
        status = self.wait_moved()
        self.assertEqual(status["on_drive"], ["a", "b"])
        self.assertTrue((self.drive / "novels" / "a" / "chapters" / "000001.zst").is_file())
        self.assertEqual([p.name for p in (self.here / "novels" / "a").iterdir()], ["cover.jpg"])

    def test_moving_needs_the_drive(self) -> None:
        self.unplug()
        with self.assertRaisesRegex(ValueError, "not plugged in"):
            self.books.move(["a"], to_drive=False)

    def test_a_novel_deleted_while_unplugged_goes_from_the_drive_once_it_is_back(self) -> None:
        self.unplug()
        self.books.forget("a")
        self.assertTrue((self.drive / "novels" / "a").exists())
        (self.drive.parent / "stamp").rename(self.drive / relocation.BOOK_STAMP)
        self.books._checked = -books_module.PLUGGED_CHECK
        self.assertTrue(self.books.plugged())
        for _ in range(500):
            if not (self.drive / "novels" / "a").exists():
                break
            time.sleep(0.01)
        self.assertFalse((self.drive / "novels" / "a").exists())


class UnpluggedErrorTests(unittest.TestCase):
    def test_it_is_an_os_error_for_the_downloads(self) -> None:
        self.assertTrue(issubclass(BookDriveUnplugged, OSError))


if __name__ == "__main__":
    unittest.main()
