"""Moving the library to another drive from the app.

The rule the whole move is built on: the library is never split between two
places. The old folder is whole and in use until the new one has everything,
and afterwards a file is deleted from the old one only where the new one has it.
"""

import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from lncrawl import relocation
from lncrawl.relocation import (
    MARKER,
    STAMP,
    LibraryMissing,
    MoveStopped,
    begin_move,
    choose_target,
    cleanup_done,
    copy_done,
    copy_tree,
    read_marker,
    remove_old_copy,
    resolve,
    switch,
    write_marker,
)

ROOT = Path(__file__).resolve().parents[1]


def nothing(files: int, size: int) -> None:
    pass


def tree(root: Path) -> dict:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def library(folder: Path) -> None:
    """A small data folder: two chapters, an export, a database and settings."""
    for relative, content in {
        "novels/a/chapters/000001.zst": b"chapter one",
        "novels/a/chapters/000002.zst": b"chapter two, longer",
        "novels/a/cover.jpg": b"jpg",
        "exports/job.zip": b"zip",
        "config.json": b'{"database": {}}',
        "tmp/work/scratch": b"working file",
        "logs/app.log": b"log",
    }.items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    with sqlite3.connect(folder / "sqlite.db") as database:
        database.execute("create table novels (title text)")
        database.execute("insert into novels values ('Moved Novel')")


class CopyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old = Path(self.temp.name) / "c" / "LNCrawl"
        self.new = Path(self.temp.name) / "ssd" / "LNCrawl"
        library(self.old)

    def copy(self, stop=None) -> list:
        copied = []
        real = relocation.shutil.copy2

        def counted(source, target, **kwargs):
            copied.append(Path(source).name)
            return real(source, target, **kwargs)

        with patch.object(relocation.shutil, "copy2", counted):
            copy_tree(self.old, self.new, (*relocation.LATE, *relocation.SKIPPED), nothing, stop)
        return copied

    def test_the_first_copy_leaves_what_the_running_app_holds_open(self) -> None:
        before = tree(self.old)
        self.copy()
        self.assertEqual(
            sorted(tree(self.new)),
            [
                "exports/job.zip",
                "novels/a/chapters/000001.zst",
                "novels/a/chapters/000002.zst",
                "novels/a/cover.jpg",
            ],
        )
        self.assertEqual(tree(self.old), before)  # nothing in the old folder is touched

    def test_a_copy_that_was_stopped_carries_on_where_it_was(self) -> None:
        self.assertEqual(len(self.copy()), 4)
        self.assertEqual(self.copy(), [])  # all there already: nothing is copied twice
        chapter = self.old / "novels/a/chapters/000002.zst"
        chapter.write_bytes(b"chapter two, downloaded again")
        self.assertEqual(self.copy(), ["000002.zst"])  # only the one that changed
        self.assertEqual(
            (self.new / "novels/a/chapters/000002.zst").read_bytes(),
            b"chapter two, downloaded again",
        )

    def test_a_copy_keeps_each_files_time(self) -> None:
        chapter = self.old / "novels/a/chapters/000001.zst"
        os.utime(chapter, (1_700_000_000, 1_700_000_000))
        self.copy()
        self.assertEqual((self.new / "novels/a/chapters/000001.zst").stat().st_mtime, 1_700_000_000)

    def test_asked_to_stop_it_stops_and_leaves_no_half_written_file(self) -> None:
        stop = Event()
        stop.set()
        with self.assertRaises(MoveStopped):
            self.copy(stop)
        self.assertEqual([p for p in self.new.rglob("*.moving")] if self.new.exists() else [], [])


class DriveTroubleTests(unittest.TestCase):
    """A long copy to an external drive: 790 GB stopped at 3 GB when the drive blinked."""

    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old = Path(self.temp.name) / "c" / "LNCrawl"
        self.new = Path(self.temp.name) / "ssd" / "LNCrawl"
        library(self.old)
        self.skip = (*relocation.LATE, *relocation.SKIPPED)
        for name, value in (("RETRY_FIRST_WAIT", 0.0), ("RETRY_PATIENCE", 5.0)):
            patcher = patch.object(relocation, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def failing(self, times: int, error: OSError):
        """`shutil.copy2` that fails ``times`` times before it works again."""
        real = relocation.shutil.copy2
        calls = []

        def copy2(source, target, **kwargs):
            calls.append(Path(source).name)
            if len(calls) <= times:
                raise error
            return real(source, target, **kwargs)

        patcher = patch.object(relocation.shutil, "copy2", copy2)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    def test_a_drive_that_drops_out_for_a_moment_is_waited_for(self) -> None:
        gone = OSError(19, "A device which does not exist was specified")
        self.failing(3, gone)
        heard = []
        copy_tree(self.old, self.new, self.skip, nothing, waiting=heard.append)
        self.assertEqual(len(tree(self.new)), 4)  # everything arrived all the same
        self.assertEqual(heard, [gone, gone, gone, None])  # said so, then said it was back

    def test_a_drive_that_stays_away_ends_the_copy_with_the_reason(self) -> None:
        gone = OSError(19, "A device which does not exist was specified")
        self.failing(10**6, gone)
        before = tree(self.old)
        with patch.object(relocation, "RETRY_PATIENCE", 0.05):
            with self.assertRaises(OSError) as caught:
                copy_tree(self.old, self.new, self.skip, nothing)
        self.assertIs(caught.exception, gone)
        self.assertEqual(tree(self.old), before)

    def test_a_full_drive_is_not_waited_for(self) -> None:
        import errno

        calls = self.failing(10**6, OSError(errno.ENOSPC, "No space left on device"))
        with self.assertRaises(OSError):
            copy_tree(self.old, self.new, self.skip, nothing)
        self.assertEqual(len(calls), 1)

    def test_asked_to_stop_while_waiting_for_the_drive_it_stops(self) -> None:
        stop = Event()
        self.failing(10**6, OSError(19, "A device which does not exist was specified"))
        with self.assertRaises(MoveStopped):
            copy_tree(self.old, self.new, self.skip, nothing, stop, waiting=lambda e: stop.set())

    def test_small_files_are_counted_at_the_room_they_take_on_the_new_drive(self) -> None:
        # exFAT on a big drive stores files in 128 KB blocks: 1.67 million page
        # images take about 100 GB more there than their sizes add up to.
        size = relocation.measure(self.old, self.skip, nothing, unit=128 * 1024)
        self.assertEqual(size.files, 4)
        self.assertEqual(size.bytes, len(b"chapter one") + len(b"chapter two, longer") + 3 + 3)
        self.assertEqual(size.on_disk, 4 * 128 * 1024)
        self.assertEqual(relocation.measure(self.old, self.skip, nothing).on_disk, size.bytes)
        self.assertGreaterEqual(relocation.allocation_unit(self.old), 512)


class SwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old = Path(self.temp.name) / "c" / "LNCrawl"
        self.new = Path(self.temp.name) / "ssd" / "LNCrawl"
        library(self.old)

    def copied(self) -> None:
        begin_move(self.old, self.new)
        copy_tree(self.old, self.new, (*relocation.LATE, *relocation.SKIPPED), nothing)
        (self.new / STAMP).write_text("")
        copy_done(self.old)

    def test_until_the_copy_is_done_the_old_folder_is_the_library(self) -> None:
        self.assertEqual(resolve(self.old), self.old)
        begin_move(self.old, self.new)
        self.assertEqual(resolve(self.old), self.old)

    def test_the_next_start_finishes_the_move_and_uses_the_new_folder(self) -> None:
        self.copied()
        self.assertEqual(resolve(self.old), self.new)
        with sqlite3.connect(self.new / "sqlite.db") as database:
            self.assertEqual(
                database.execute("select title from novels").fetchall(), [("Moved Novel",)]
            )
        self.assertEqual((self.new / "config.json").read_bytes(), b'{"database": {}}')
        self.assertEqual((self.new / "logs/app.log").read_bytes(), b"log")
        self.assertEqual(
            read_marker(self.old),
            {"path": str(self.new), "state": "moved", "cleanup": True, "old": str(self.old)},
        )
        self.assertEqual(resolve(self.old), self.new)  # and every start after it

    def test_the_database_is_copied_whole_while_something_still_writes_to_it(self) -> None:
        self.copied()
        writer = sqlite3.connect(self.old / "sqlite.db")
        self.addCleanup(writer.close)
        writer.execute("pragma journal_mode=wal")
        writer.execute("insert into novels values ('Written before the switch')")
        writer.commit()
        writer.execute("insert into novels values ('Still being written')")  # not committed
        switch(self.old, self.old, self.new)
        with sqlite3.connect(self.new / "sqlite.db") as database:
            titles = [row[0] for row in database.execute("select title from novels")]
        self.assertEqual(titles, ["Moved Novel", "Written before the switch"])

    def test_a_copy_that_went_missing_is_not_switched_to(self) -> None:
        self.copied()
        before = tree(self.old)
        (self.new / STAMP).unlink()  # the drive came back without it, or it was deleted
        with patch.object(sys, "stderr", MagicMock()):
            self.assertEqual(resolve(self.old), self.old)
        self.assertEqual(read_marker(self.old), {})
        before.pop(MARKER)
        self.assertEqual(tree(self.old), before)

    def test_a_library_moved_once_can_be_moved_again(self) -> None:
        self.copied()
        resolve(self.old)  # now in self.new
        remove_old_copy(self.old, self.new, nothing)
        cleanup_done(self.old)
        third = Path(self.temp.name) / "bigger" / "LNCrawl"
        begin_move(self.old, third)
        # stopped part way: the library is still where the first move put it
        self.assertEqual(resolve(self.old), self.new)
        copy_tree(self.new, third, (*relocation.LATE, *relocation.SKIPPED), nothing)
        (third / STAMP).write_text("")
        copy_done(self.old)
        self.assertEqual(resolve(self.old), third)
        self.assertEqual(read_marker(self.old)["old"], str(self.new))
        with sqlite3.connect(third / "sqlite.db") as database:
            self.assertEqual(database.execute("select count(*) from novels").fetchone(), (1,))

    def test_a_moved_library_whose_drive_is_gone_is_an_error_not_an_empty_library(self) -> None:
        write_marker(self.old, path=str(self.new), state="moved")
        with self.assertRaises(LibraryMissing) as caught:
            resolve(self.old)
        self.assertIn(str(self.new), str(caught.exception))

    def test_a_download_process_does_not_finish_a_move_under_its_app(self) -> None:
        self.copied()
        with patch.object(relocation.multiprocessing, "parent_process", return_value=object()):
            self.assertEqual(resolve(self.old), self.old)
        self.assertIs(read_marker(self.old)["copied"], True)  # left for the app itself


class RemoveOldCopyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old = Path(self.temp.name) / "c" / "LNCrawl"
        self.new = Path(self.temp.name) / "ssd" / "LNCrawl"
        library(self.old)
        copy_tree(self.old, self.new, (*relocation.LATE, *relocation.SKIPPED), nothing)
        (self.new / STAMP).write_text("")
        switch(self.old, self.old, self.new)

    def test_the_old_copy_goes_and_the_marker_stays(self) -> None:
        freed, left = remove_old_copy(self.old, self.new, nothing)
        self.assertEqual(sorted(p.name for p in self.old.iterdir()), [MARKER])
        self.assertEqual(left, 0)
        self.assertGreater(freed, 0)
        self.assertEqual(resolve(self.old), self.new)

    def test_what_the_running_app_kept_writing_is_carried_over_at_the_restart(self) -> None:
        # The app goes on fetching source files and settings while the novels
        # are copied; a real run left 55 of them behind in the old folder.
        old, new = Path(self.temp.name) / "c2" / "LNCrawl", Path(self.temp.name) / "ssd2"
        library(old)
        begin_move(old, new)
        copy_tree(old, new, (*relocation.LATE, *relocation.SKIPPED), nothing)
        (new / STAMP).write_text("")
        copy_done(old)
        source = old / "sources/en/s/site.py"  # arrived after the copy
        source.parent.mkdir(parents=True)
        source.write_bytes(b"crawler")
        self.assertEqual(resolve(old), new)
        self.assertEqual((new / "sources/en/s/site.py").read_bytes(), b"crawler")
        self.assertEqual(remove_old_copy(old, new, nothing)[1], 0)
        self.assertEqual(sorted(p.name for p in old.iterdir()), [MARKER])

    def test_a_file_the_new_folder_does_not_have_is_kept(self) -> None:
        late = self.old / "novels/b/chapters/000001.zst"  # downloaded after the copy
        late.parent.mkdir(parents=True)
        late.write_bytes(b"only here")
        (self.new / "novels/a/chapters/000002.zst").write_bytes(b"cut short")  # a bad copy
        _, left = remove_old_copy(self.old, self.new, nothing)
        self.assertEqual(left, 2)
        self.assertEqual(late.read_bytes(), b"only here")
        self.assertEqual(
            (self.old / "novels/a/chapters/000002.zst").read_bytes(), b"chapter two, longer"
        )
        self.assertFalse((self.old / "novels/a/chapters/000001.zst").exists())


class ChooseTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.current = self.root / "c" / "LNCrawl"
        self.current.mkdir(parents=True)

    def test_a_new_or_empty_folder_is_used_as_it_is(self) -> None:
        self.assertEqual(
            choose_target(self.current, self.root / "ssd" / "Books"), self.root / "ssd" / "Books"
        )
        (self.root / "empty").mkdir()
        self.assertEqual(choose_target(self.current, self.root / "empty"), self.root / "empty")

    def test_a_folder_with_other_things_in_it_gets_a_folder_of_its_own_inside(self) -> None:
        drive = self.root / "ssd"
        (drive / "Games").mkdir(parents=True)
        self.assertEqual(choose_target(self.current, drive), drive / "LNCrawl")

    def test_an_earlier_try_at_the_move_is_carried_on_from(self) -> None:
        earlier = self.root / "ssd" / "LNCrawl"
        (earlier / "novels").mkdir(parents=True)
        (earlier / STAMP).write_text("")
        self.assertEqual(choose_target(self.current, earlier), earlier)
        self.assertEqual(choose_target(self.current, earlier.parent), earlier)

    def test_places_that_cannot_be_used_are_refused_with_the_reason(self) -> None:
        (self.current / "novels").mkdir()
        taken = self.root / "ssd" / "LNCrawl"
        (taken / "photos").mkdir(parents=True)
        for folder, reason in (
            (Path("relative/folder"), "full path"),
            (self.current, "already in"),
            (self.current / "novels" / "inside", "already in"),
            (self.current.parent, "already in"),  # would land on the library itself
            (taken.parent, "other files"),
        ):
            with self.subTest(folder=folder):
                with self.assertRaises(ValueError) as caught:
                    choose_target(self.current, folder)
                self.assertIn(reason, str(caught.exception))


class ServiceTests(unittest.TestCase):
    """The part that runs in the app: pause downloads, copy, wait for the restart."""

    def setUp(self) -> None:
        from lncrawl.services import library_move

        self.module = library_move
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.old = Path(self.temp.name) / "c" / "LNCrawl"
        self.ssd = Path(self.temp.name) / "ssd"
        library(self.old)
        self.service = self.app(self.old)

    def app(self, folder: Path):
        """The service as the app running from ``folder`` has it."""
        self.scheduler = SimpleNamespace(running=True, stop=MagicMock(), start=MagicMock())
        ctx = SimpleNamespace(
            config=SimpleNamespace(app=SimpleNamespace(app_dir=folder, base_dir=self.old)),
            scheduler=self.scheduler,
        )
        patcher = patch.object(self.module, "ctx", ctx)
        patcher.start()
        self.addCleanup(patcher.stop)
        service = self.module.LibraryMoveService()
        self.addCleanup(service.close)
        return service

    def wait(self, done, service=None) -> dict:
        service = service or self.service
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            status = service.status()
            if done(status):
                return status
            time.sleep(0.01)
        self.fail(f"still {service.status()}")

    def moved(self) -> Path:
        self.service.start(str(self.ssd))
        status = self.wait(lambda s: s["state"] in ("copied", "failed"))
        self.assertEqual(status["state"], "copied", status["error"])
        return Path(status["target"])

    def test_the_copy_runs_with_downloads_paused_and_waits_for_a_restart(self) -> None:
        before = tree(self.old)
        target = self.moved()
        self.assertEqual(target, self.ssd)
        status = self.service.status()
        self.assertEqual((status["files"], status["total_files"]), (4, 4))
        self.scheduler.stop.assert_called_once()
        self.scheduler.start.assert_not_called()  # paused until the restart
        self.assertEqual(status["folder"], str(self.old))  # still the library
        marker = read_marker(self.old)
        self.assertEqual((marker["moving_to"], marker["copied"]), (str(target), True))
        after = tree(self.old)
        after.pop(MARKER)
        self.assertEqual(after, before)
        self.assertEqual(resolve(self.old), target)  # the restart

    def test_cancelled_after_the_copy_the_library_stays_and_downloads_resume(self) -> None:
        self.moved()
        self.assertEqual(self.service.cancel()["state"], "idle")
        self.scheduler.start.assert_called_once()
        self.assertEqual(read_marker(self.old), {})
        self.assertEqual(resolve(self.old), self.old)

    def test_a_drive_without_room_is_refused_before_anything_is_copied(self) -> None:
        usage = SimpleNamespace(free=10 * 1024 * 1024)
        with (
            patch.object(self.module.shutil, "disk_usage", return_value=usage),
            self.assertLogs(self.module.logger, "ERROR"),
        ):
            self.service.start(str(self.ssd))
            status = self.wait(lambda s: s["state"] in ("copied", "failed"))
        self.assertEqual(status["state"], "failed")
        self.assertIn("free", status["error"])
        self.assertFalse((self.ssd / "novels").exists())
        self.scheduler.start.assert_called_once()  # downloads carry on where the library is
        self.assertEqual(resolve(self.old), self.old)

    def test_a_drive_with_big_blocks_needs_more_room_than_the_sizes_add_up_to(self) -> None:
        usage = SimpleNamespace(free=int(2.5 * 1024**3))  # plenty for four small files
        with (
            patch.object(self.module.shutil, "disk_usage", return_value=usage),
            patch.object(self.module, "allocation_unit", return_value=512 * 1024**2),
            self.assertLogs(self.module.logger, "ERROR"),
        ):
            self.service.start(str(self.ssd))
            status = self.wait(lambda s: s["state"] in ("copied", "failed"))
        self.assertEqual(status["state"], "failed")
        self.assertIn("blocks", status["error"])
        self.assertFalse((self.ssd / "novels").exists())

    def test_a_drive_that_stops_answering_is_waited_for_and_the_card_says_so(self) -> None:
        real = relocation.shutil.copy2
        seen = Event()
        calls = []

        def copy2(source, target, **kwargs):
            calls.append(source)
            if len(calls) == 1:
                raise OSError(19, "A device which does not exist was specified")
            seen.wait(10)  # the drive is back once the card has shown the wait
            return real(source, target, **kwargs)

        with (
            patch.object(relocation.shutil, "copy2", copy2),
            patch.object(relocation, "RETRY_FIRST_WAIT", 0.0),
            self.assertLogs(self.module.logger, "WARNING"),
        ):
            self.service.start(str(self.ssd))
            waiting = self.wait(lambda s: s["problem"] or s["state"] in ("copied", "failed"))
            self.assertEqual(waiting["state"], "copying")
            self.assertIn("device which does not exist", waiting["problem"])
            seen.set()
            status = self.wait(lambda s: s["state"] in ("copied", "failed"))
        self.assertEqual(status["state"], "copied", status["error"])
        self.assertIsNone(status["problem"])
        self.assertEqual((status["files"], status["total_files"]), (4, 4))

    def test_a_second_move_is_refused_while_one_waits_for_its_restart(self) -> None:
        self.moved()
        with self.assertRaises(ValueError):
            self.service.start(str(Path(self.temp.name) / "other"))

    def test_a_copy_the_last_run_did_not_finish_is_dropped_at_the_next_start(self) -> None:
        begin_move(self.old, self.ssd)  # the app was closed part way through
        self.assertEqual(resolve(self.old), self.old)
        self.app(self.old).on_startup()
        self.assertEqual(read_marker(self.old), {})

    def test_after_the_restart_the_old_copy_is_removed(self) -> None:
        target = self.moved()
        self.assertEqual(resolve(self.old), target)  # the restart
        (target / "logs" / "app.log").write_bytes(b"log, and what the new run added")
        restarted = self.app(target)
        restarted.on_startup()
        status = self.wait(
            lambda s: (s["old_copy"] or {}).get("state") in ("done", "failed"), restarted
        )
        self.assertEqual(status["old_copy"]["state"], "done", status["old_copy"])
        self.assertEqual(status["old_copy"]["left"], 0)
        self.assertGreater(status["old_copy"]["freed"], 0)
        self.assertEqual(sorted(p.name for p in self.old.iterdir()), [MARKER])
        self.assertEqual(read_marker(self.old), {"path": str(target), "state": "moved"})
        self.assertEqual(
            (target / "novels/a/chapters/000002.zst").read_bytes(), b"chapter two, longer"
        )


FIRST_RUN = """
from lncrawl.context import ctx
ctx.setup(sync_remote_index=False)
from lncrawl.dao import Novel
with ctx.db.session() as session:
    session.add(Novel(id="moved", url="https://example.com/moved", domain="example.com",
                      title="Moved Novel"))
    session.commit()
ctx.files.save_text("novels/moved/chapters/000001.zst", "<p>The story so far.</p>")
ctx.config.crawler.disk_size_limit = 0  # any setting saved writes config.json
ctx.destroy()
"""

MOVE = """
import sys, time
from lncrawl.context import ctx
ctx.setup(sync_remote_index=False)
ctx.library_move.start(sys.argv[1])
while ctx.library_move.status()["state"] not in ("copied", "failed"):
    time.sleep(0.05)
status = ctx.library_move.status()
print("STATE", status["state"], status["error"])
print("TARGET", status["target"])
ctx.destroy()
"""

AFTER_RESTART = """
import time
from lncrawl.context import ctx
ctx.setup(sync_remote_index=False)
from lncrawl.dao import Novel
with ctx.db.session() as session:
    novel = session.get(Novel, "moved")
print("FOLDER", ctx.config.app.app_dir)
print("NOVEL", novel.title if novel else None)
print("CHAPTER", ctx.files.load_text("novels/moved/chapters/000001.zst"))
print("DATABASE", ctx.config.db.url)
ctx.library_move.on_startup()
while (ctx.library_move.status()["old_copy"] or {}).get("state") not in ("done", "failed"):
    time.sleep(0.05)
print("OLD", ctx.library_move.status()["old_copy"]["state"])
ctx.destroy()
"""


def run_app(code: str, data: Path, *args: str) -> str:
    env = dict(os.environ, LNCRAWL_DATA_PATH=str(data))
    env.pop("DATABASE_URL", None)
    result = subprocess.run(
        [sys.executable, "-c", code, *args],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"exit {result.returncode}: " + (result.stdout + result.stderr)[-4000:]
        )
    return result.stdout


class RealAppTests(unittest.TestCase):
    def test_a_library_moved_from_the_app_is_all_there_after_the_restart(self) -> None:
        with TemporaryDirectory() as temp:
            old, drive = Path(temp) / "c" / "LNCrawl", Path(temp) / "ssd"
            (drive / "Games").mkdir(parents=True)  # a drive with other things on it
            run_app(FIRST_RUN, old)

            output = run_app(MOVE, old, str(drive))
            target = drive / "LNCrawl"
            self.assertIn("STATE copied None", output)
            self.assertIn(f"TARGET {target}", output)
            self.assertTrue((old / "sqlite.db").is_file())  # still the library until a restart

            output = run_app(AFTER_RESTART, old)  # started the same way as ever
            self.assertIn(f"FOLDER {target}", output)
            self.assertIn("NOVEL Moved Novel", output)
            self.assertIn("CHAPTER <p>The story so far.</p>", output)
            self.assertIn(f"DATABASE sqlite:///{target.resolve().as_posix()}/sqlite.db", output)
            self.assertIn("OLD done", output)
            self.assertEqual(sorted(p.name for p in old.iterdir()), [MARKER])
            self.assertEqual(sorted(p.name for p in drive.iterdir()), ["Games", "LNCrawl"])

            output = run_app(
                AFTER_RESTART.replace("ctx.library_move.on_startup()", "pass")
                .replace(
                    'while (ctx.library_move.status()["old_copy"] or {}).get("state") not in ("done", "failed"):\n    time.sleep(0.05)\n',
                    "",
                )
                .replace('print("OLD", ctx.library_move.status()["old_copy"]["state"])', ""),
                old,
            )
            self.assertIn(f"FOLDER {target}", output)  # and on every start after that

    def test_a_moved_library_whose_drive_is_unplugged_stops_the_app_with_the_reason(self) -> None:
        with TemporaryDirectory() as temp:
            old, gone = Path(temp) / "c" / "LNCrawl", Path(temp) / "unplugged" / "LNCrawl"
            write_marker(old, path=str(gone), state="moved")
            result = subprocess.run(
                [sys.executable, "-c", "import lncrawl.config"],
                env=dict(os.environ, LNCRAWL_DATA_PATH=str(old)),
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(f"The library was moved to {gone}, which cannot be found", result.stderr)
            self.assertFalse((old / "sqlite.db").exists())  # no empty library was started


if __name__ == "__main__":
    unittest.main()
