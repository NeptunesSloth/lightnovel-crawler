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

    def test_each_step_shows_a_bar(self) -> None:
        heard: list = []
        relocation.split(self.base, self.drive, self.here, heard.append)
        for step in ("Step 1 of 3", "Step 2 of 3", "Step 3 of 3"):
            self.assertTrue(any(step in text and "100%" in text for text in heard), (step, heard))
        settings = [text for text in heard if "Step 2 of 3" in text and "files" in text]
        self.assertIn("[" + "#" * 30 + "]", settings[-1])

    def test_a_database_another_copy_has_open_stops_it_before_anything_is_copied(self) -> None:
        relocation.begin_split(self.base, self.drive, self.here)
        real = relocation.os.replace

        def in_use(source, target):
            if str(source).endswith("sqlite.db") and str(target).endswith(".split-check"):
                raise PermissionError(32, "being used by another process")
            return real(source, target)

        with patch.object(relocation.os, "replace", in_use):
            self.assertEqual(relocation.resolve(self.base), self.drive)
        self.assertIn("another copy", relocation.read_marker(self.base)["split_failed"])
        self.assertFalse((self.here / "sqlite.db").exists())  # nothing was copied
        self.assertTrue((self.drive / "sqlite.db").is_file())

    def test_a_failure_at_the_very_end_still_leaves_one_whole_library(self) -> None:
        # The drive's database could not be renamed: the library is here all
        # the same, and the app does not start again from a drive with none.
        relocation.begin_split(self.base, self.drive, self.here)
        real = relocation.os.replace

        def refused(source, target):
            if ".before-split-" in str(target) and str(source).startswith(str(self.drive)):
                raise PermissionError(5, "Access is denied")
            return real(source, target)

        with (
            patch.object(relocation, "_database_free"),
            patch.object(relocation.os, "replace", refused),
        ):
            self.assertEqual(relocation.resolve(self.base), self.here)
        self.assertEqual(relocation.resolve(self.base), self.here)
        database = sqlite3.connect(str(self.here / "sqlite.db"))
        self.assertEqual(database.execute("SELECT id FROM novels").fetchall(), [("a",)])
        database.close()

    def test_a_second_try_never_copies_over_the_good_copy_with_nothing(self) -> None:
        relocation.begin_split(self.base, self.drive, self.here)
        self.assertEqual(relocation.resolve(self.base), self.here)
        # asked again by mistake, with the drive's database already set aside
        relocation.point_at(self.base, self.drive)
        relocation.begin_split(self.base, self.drive, self.here)
        self.assertEqual(relocation.resolve(self.base), self.drive)
        self.assertIn("no database", relocation.read_marker(self.base)["split_failed"])
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
        marker = relocation.read_marker(self.base)
        self.assertNotIn("split_to", marker)
        self.assertIn("disk full", marker["split_failed"])  # kept for Tools to show
        self.assertIn("disk full", (self.drive / relocation.MOVE_LOG).read_text())
        relocation.begin_split(self.base, self.drive, self.here)  # a new try forgets it
        self.assertNotIn("split_failed", relocation.read_marker(self.base))


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
        # listed only once its folder is really there: looking up a file of a
        # novel that was deleted must not put it back in the list
        saved = json.loads((self.here / relocation.BOOKS).read_text())
        self.assertNotIn("new", saved["novels"])
        (self.drive / "novels" / "new" / "chapters").mkdir(parents=True)
        self.route("novels/new/chapters/000001.zst")
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

    def test_kept_here_the_drive_keeps_its_copy_and_gets_what_is_new_back(self) -> None:
        self.books.move(["a"], to_drive=False)
        status = self.wait_moved()
        self.assertEqual(status["on_drive"], ["b"])
        self.assertEqual(status["copies"], ["a"])
        self.assertTrue((self.here / "novels" / "a" / "chapters" / "000001.zst").is_file())
        # the drive still holds the whole library
        self.assertTrue((self.drive / "novels" / "a" / "chapters" / "000001.zst").is_file())
        self.assertEqual(
            self.route("novels/a/chapters/000001.zst"),
            self.here / "novels" / "a" / "chapters" / "000001.zst",
        )
        self.unplug()
        self.assertFalse(self.books.unavailable("a"))  # readable without the drive
        (self.here / "novels" / "a" / "chapters" / "000002.zst").write_bytes(b"new")
        (self.drive.parent / "stamp").rename(self.drive / relocation.BOOK_STAMP)
        self.books._checked = -books_module.PLUGGED_CHECK
        self.books.move(["a"], to_drive=True)
        status = self.wait_moved()
        self.assertEqual(status["on_drive"], ["a", "b"])
        self.assertEqual(status["copies"], [])
        self.assertTrue((self.drive / "novels" / "a" / "chapters" / "000002.zst").is_file())
        self.assertEqual([p.name for p in (self.here / "novels" / "a").iterdir()], ["cover.jpg"])

    def test_brought_here_without_a_copy_it_leaves_the_drive(self) -> None:
        self.books.move(["a"], to_drive=False, keep_copy=False)
        status = self.wait_moved()
        self.assertEqual(status["copies"], [])
        self.assertTrue((self.here / "novels" / "a" / "chapters" / "000001.zst").is_file())
        self.assertFalse((self.drive / "novels" / "a").exists())

    def test_a_novel_with_a_copy_on_the_drive_is_deleted_from_both(self) -> None:
        self.books.move(["a"], to_drive=False)
        self.wait_moved()
        self.assertEqual(
            self.books.folders("a"),
            [self.here / "novels" / "a", self.drive / "novels" / "a"],
        )
        self.books.forget("a")
        self.assertEqual(self.books.status()["copies"], [])

    def test_a_drive_that_drops_out_mid_copy_does_not_cost_the_only_copy(self) -> None:
        # the copy skips what it cannot find; the novel must stay on the drive
        real = books_module.copy_tree

        def dropped(source, target, *args, **kwargs):
            return None  # the drive answered nothing: "nothing to copy"

        with patch.object(books_module, "copy_tree", dropped):
            self.books.move(["a"], to_drive=False, keep_copy=False)
            status = self.wait_moved()
        self.assertEqual(status["moving"]["failed"], 1)
        self.assertIn("did not arrive", status["moving"]["error"])
        self.assertEqual(status["on_drive"], ["a", "b"])
        self.assertTrue((self.drive / "novels" / "a" / "chapters" / "000001.zst").is_file())
        self.assertIsNotNone(real)

    def test_a_file_written_during_the_move_is_not_lost(self) -> None:
        real = books_module.copy_tree
        calls = []

        def late_write(source, target, *args, **kwargs):
            real(source, target, *args, **kwargs)
            if not calls:  # a download saves a chapter just after the first pass
                (source / "chapters" / "000002.zst").write_bytes(b"late")
            calls.append(1)

        with patch.object(books_module, "copy_tree", late_write):
            self.books.move(["a"], to_drive=False, keep_copy=False)
            self.wait_moved()
        self.assertEqual(
            (self.here / "novels" / "a" / "chapters" / "000002.zst").read_bytes(), b"late"
        )
        self.assertFalse((self.drive / "novels" / "a").exists())

    def test_a_change_made_by_another_process_is_seen(self) -> None:
        other = BookDrive()  # a download worker's own copy
        self.assertTrue(other.on_drive("a"))
        self.books.move(["a"], to_drive=False, keep_copy=False)
        self.wait_moved()
        other._looked = -books_module.RELOAD_CHECK
        self.assertFalse(other.on_drive("a"))

    def test_an_unreadable_list_is_not_taken_for_no_drive(self) -> None:
        self.assertTrue(self.books.configured)
        (self.here / relocation.BOOKS).write_text("{ cut short")
        self.books._looked = -books_module.RELOAD_CHECK
        self.assertTrue(self.books.configured)
        self.assertTrue(self.books.on_drive("a"))

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
