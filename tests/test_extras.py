import os
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import test_novel_repair

from lncrawl.dao import Artifact, Chapter, ChapterImage, Library, LibraryNovel, Novel
from lncrawl.enums import OutputFormat
from lncrawl.services import extras

PAGE = "a" * 32
OLD = time.time() - 30 * 24 * 3600


class ExtrasTests(unittest.TestCase):
    """Duplicate novels, old e-books and leftover files are found, then cleaned up."""

    def setUp(self) -> None:
        fixture = test_novel_repair.NovelRepairTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.ctx = fixture.ctx
        self.ctx.config.app.base_dir = self.ctx.config.app.app_dir
        self.ctx.recommendations = SimpleNamespace(invalidate=MagicMock(), index_remove=MagicMock())
        fixture.stack.enter_context(patch.object(extras, "ctx", self.ctx))

    def novel(self, novel_id: str, title: str, authors: str = "", manga: bool = False) -> None:
        with self.ctx.db.session() as sess:
            sess.add(
                Novel(
                    id=novel_id,
                    title=title,
                    authors=authors,
                    manga=manga,
                    url=f"https://site.test/{novel_id}",
                    domain="site.test",
                )
            )
            sess.commit()

    def chapter(self, novel_id: str, serial: int, body: str) -> None:
        with self.ctx.db.session() as sess:
            sess.add(
                Chapter(
                    id=f"{novel_id}-{serial}",
                    novel_id=novel_id,
                    serial=serial,
                    title=f"Chapter {serial}",
                    url=f"https://site.test/{novel_id}/{serial}",
                    is_done=True,
                )
            )
            sess.commit()
        self.ctx.files.save_text(f"novels/{novel_id}/chapters/{serial:06}.zst", body)

    def run_clean(self, *kinds: str) -> dict:
        extras.start(list(kinds))
        for _ in range(200):
            if not extras.status()["running"]:
                break
            time.sleep(0.05)
        return extras.status()

    def test_copies_of_one_story_are_found_and_different_stories_are_not(self) -> None:
        self.novel("a1", "Real", "Takehiko Inoue", manga=True)
        self.novel("a2", "REAL", "takehiko inoue", manga=True)
        self.novel("x1", "Real", "", manga=True)  # names no author: could be either story
        self.novel("b1", "Real", "Someone Else", manga=True)
        self.novel("c1", "Real")  # a novel, not the manga
        self.novel("d1", "Vagabond", manga=True)
        self.novel("d2", "vagabond", manga=True)
        for _ in range(5):  # the same answer every time
            sets = extras._duplicate_sets()
            found = sorted(sorted(c["id"] for c in copies) for copies in sets)
            self.assertEqual(found, [["a1", "a2"], ["d1", "d2"]])

    def test_merging_keeps_the_pages_and_shelves_of_the_copy_that_goes(self) -> None:
        self.novel("kept", "Real", manga=True)
        self.novel("gone", "Real", manga=True)
        page = f'<img src="images/{PAGE}.jpg"/>'
        for serial in (1, 2):
            self.chapter("kept", serial, page)
        self.chapter("gone", 1, page)
        file = self.ctx.files.resolve(f"novels/gone/images/{PAGE}.jpg")
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"page")
        with self.ctx.db.session() as sess:
            sess.add(ChapterImage(id=PAGE, url="x", chapter_id="gone-1", novel_id="gone"))
            sess.add(Library(id="shelf", user_id="user", name="Sports"))
            sess.add(LibraryNovel(library_id="shelf", novel_id="gone"))
            sess.commit()
        with patch.object(self.ctx.novels, "heal_from_library", return_value={"healed": 0}):
            done = self.run_clean("duplicates")
        self.assertEqual(done["merged"], 1, done)
        with self.ctx.db.session() as sess:
            self.assertIsNone(sess.get(Novel, "gone"))
            image = sess.get(ChapterImage, PAGE)
            assert image is not None
            self.assertEqual(image.novel_id, "kept")
            self.assertIsNotNone(sess.get(LibraryNovel, ("shelf", "kept")))
        self.assertTrue(self.ctx.files.resolve(f"novels/kept/images/{PAGE}.jpg").is_file())

    def test_only_the_newest_e_book_of_a_novel_is_kept(self) -> None:
        folder = self.ctx.files.resolve("novels/novel/artifacts")
        folder.mkdir(parents=True, exist_ok=True)
        for name in ("Old Name.epub", "Test Novel.epub", "stray.epub"):
            (folder / name).write_bytes(b"book")
            os.utime(folder / name, (OLD, OLD))
        with self.ctx.db.session() as sess:
            for at, name in ((1, "Old Name.epub"), (2, "Test Novel.epub"), (3, "Test Novel.epub")):
                sess.add(
                    Artifact(
                        id=f"art-{at}",
                        novel_id="novel",
                        format=OutputFormat.epub,
                        file_name=name,
                        created_at=at,
                    )
                )
            sess.commit()
        rows, files = extras._old_ebooks()
        self.assertEqual(sorted(rows), ["art-1", "art-2"])
        self.assertEqual(sorted(f.name for f in files), ["Old Name.epub", "stray.epub"])
        self.run_clean("ebooks")
        self.assertEqual([p.name for p in folder.iterdir()], ["Test Novel.epub"])
        with self.ctx.db.session() as sess:
            self.assertIsNotNone(sess.get(Artifact, "art-3"))
            self.assertIsNone(sess.get(Artifact, "art-1"))

    def test_leftovers_are_old_working_files_and_folders_of_deleted_novels(self) -> None:
        root = self.ctx.config.app.app_dir
        gone = root / "novels" / "deleted-novel"
        fresh = root / "novels" / "being-added"
        work = root / "tmp" / "old-build"
        aside = root / "sqlite.db.before-split-20260101"
        new_aside = root / "sqlite.db.unreadable-20261124"
        for folder in (gone, fresh, work):
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "f").write_bytes(b"x")
        aside.write_bytes(b"db")
        new_aside.write_bytes(b"db")
        for path in (gone, work, aside):
            os.utime(path, (OLD, OLD))
        found = {path.name for path, _ in extras._leftovers()}
        self.assertEqual(found, {"deleted-novel", "old-build", aside.name})
        self.run_clean("leftovers")
        self.assertFalse(gone.exists())
        self.assertTrue(fresh.exists())  # may be a novel being added right now
        self.assertTrue(new_aside.exists())  # set aside too recently
        self.assertTrue((root / "novels" / "novel").exists())


if __name__ == "__main__":
    unittest.main()
