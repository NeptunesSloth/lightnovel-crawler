"""The database file moving to another folder while the app is using it.

The app has the database to itself for a moment, long enough to finish copying
it, and every connection after that is to the new file: sessions made earlier
included. Nothing written is lost, and nothing waiting for the database fails.
"""

from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sqlalchemy import text

from lncrawl.services import db as db_module


class DatabaseMoveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.old = root / "c" / "sqlite.db"
        self.new = root / "ssd" / "sqlite.db"
        self.old.parent.mkdir()
        self.new.parent.mkdir()
        config = SimpleNamespace(
            db=SimpleNamespace(
                url=f"sqlite:///{self.old.as_posix()}",
                pool_size=5,
                pool_timeout=30,
                pool_recycle=3600,
                connect_timeout=5,
            )
        )
        patcher = patch.object(
            db_module, "ctx", SimpleNamespace(config=config, logger=SimpleNamespace(is_debug=False))
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.db = db_module.DB()
        self.addCleanup(self.db.close)
        with self.db.session() as session:
            session.execute(text("create table novels (title text)"))
            session.execute(text("insert into novels values ('Before')"))
            session.commit()

    def titles(self, database: Path) -> list:
        with sqlite3.connect(str(database)) as connection:
            return [row[0] for row in connection.execute("select title from novels order by rowid")]

    def copy(self) -> None:
        """What the move does with the database to itself: a whole copy at the new place."""
        source = sqlite3.connect(str(self.old))
        target = sqlite3.connect(str(self.new))
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()

    def test_after_the_move_everything_is_written_to_the_new_file(self) -> None:
        early = self.db.session()  # made before the move
        self.assertTrue(self.db.move_sqlite(str(self.new), self.copy, timeout=5))
        early.execute(text("insert into novels values ('From an early session')"))
        early.commit()
        early.close()
        with self.db.session() as session:
            session.execute(text("insert into novels values ('After')"))
            session.commit()
        self.assertEqual(self.titles(self.new), ["Before", "From an early session", "After"])
        self.assertEqual(self.titles(self.old), ["Before"])

    def test_it_waits_for_work_in_progress_and_gives_up_without_changing_anything(self) -> None:
        busy = self.db.session()
        busy.execute(text("insert into novels values ('Uncommitted')"))  # holds a connection
        finished = []
        moved = self.db.move_sqlite(str(self.new), lambda: finished.append(1), timeout=0.3)
        self.assertFalse(moved)
        self.assertEqual(finished, [])  # the copy was never finished
        busy.commit()
        busy.close()
        with self.db.session() as session:  # and the database is open to everyone again
            session.execute(text("insert into novels values ('Still here')"))
            session.commit()
        self.assertEqual(self.titles(self.old), ["Before", "Uncommitted", "Still here"])

    def test_it_switches_once_the_work_in_progress_is_done(self) -> None:
        busy = self.db.session()
        busy.execute(text("insert into novels values ('Finished first')"))
        result = []
        mover = Thread(
            target=lambda: result.append(self.db.move_sqlite(str(self.new), self.copy, timeout=10))
        )
        mover.start()
        time.sleep(0.2)
        self.assertEqual(result, [])  # waiting for the work in progress
        busy.commit()
        busy.close()
        mover.join(10)
        self.assertEqual(result, [True])
        self.assertEqual(self.titles(self.new), ["Before", "Finished first"])

    def test_one_asking_while_it_switches_waits_and_then_gets_the_new_file(self) -> None:
        answered = []
        asked = Event()

        def ask() -> None:
            asked.set()
            with self.db.session() as session:
                session.execute(text("insert into novels values ('Asked meanwhile')"))
                session.commit()
            answered.append(1)

        def copy_slowly() -> None:
            Thread(target=ask).start()
            asked.wait(5)
            time.sleep(0.3)
            self.assertEqual(answered, [])  # held back, not failed
            self.copy()

        self.assertTrue(self.db.move_sqlite(str(self.new), copy_slowly, timeout=5))
        deadline = time.monotonic() + 10
        while not answered and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(answered, [1])
        self.assertEqual(self.titles(self.new), ["Before", "Asked meanwhile"])
        self.assertEqual(self.titles(self.old), ["Before"])

    def test_a_thread_holding_a_connection_can_open_another_while_a_move_waits(self) -> None:
        first = self.db.session()
        first.execute(text("select count(*) from novels"))  # one connection held
        result = []
        mover = Thread(
            target=lambda: result.append(self.db.move_sqlite(str(self.new), self.copy, timeout=10))
        )
        mover.start()
        time.sleep(0.1)
        second = Event()

        def open_another() -> None:
            with self.db.session() as session:
                session.execute(text("select count(*) from novels"))
            second.set()

        Thread(target=open_another).start()
        self.assertTrue(second.wait(5))  # not stuck behind the waiting move
        first.close()
        mover.join(10)
        self.assertEqual(result, [True])

    def test_a_failed_copy_changes_nothing(self) -> None:
        def broken() -> None:
            raise OSError("the drive went away")

        with self.assertRaisesRegex(OSError, "the drive went away"):
            self.db.move_sqlite(str(self.new), broken, timeout=5)
        with self.db.session() as session:
            session.execute(text("insert into novels values ('Still the old file')"))
            session.commit()
        self.assertEqual(self.titles(self.old), ["Before", "Still the old file"])


if __name__ == "__main__":
    unittest.main()
