"""One copy of the app per library, and a database that is missing or only unreachable."""

from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from lncrawl.utils import db_rescue, single_instance


class SingleInstanceTests(unittest.TestCase):
    def tearDown(self) -> None:
        single_instance.release()

    def test_a_second_process_cannot_take_the_same_library(self) -> None:
        with TemporaryDirectory() as temp:
            folder = Path(temp)
            self.assertTrue(single_instance.acquire(folder))
            single_instance.note_address(folder, "http://localhost:31580/")
            code = (
                "import sys; from pathlib import Path; "
                "from lncrawl.utils import single_instance as s; "
                "f = Path(sys.argv[1]); "
                "print(s.acquire(f), s.running_address(f))"
            )
            other = subprocess.run(
                [sys.executable, "-c", code, str(folder)],
                capture_output=True,
                text=True,
                cwd=Path(__file__).resolve().parent.parent,
            )
            self.assertEqual(other.stdout.strip(), "False http://localhost:31580/", other.stderr)
            single_instance.release()
            again = subprocess.run(
                [sys.executable, "-c", code, str(folder)],
                capture_output=True,
                text=True,
                cwd=Path(__file__).resolve().parent.parent,
            )
            self.assertTrue(again.stdout.startswith("True"), again.stderr)


class UnreadableTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.library = Path(self.temp.name)
        self.database = self.library / "sqlite.db"

    def test_a_database_gone_while_copies_of_it_exist_is_got_back_from_one(self) -> None:
        good = sqlite3.connect(str(self.database))
        good.execute("CREATE TABLE novels (id TEXT)")
        good.execute("INSERT INTO novels VALUES ('a')")
        good.commit()
        good.close()
        self.assertIsNotNone(db_rescue.take_snapshot(self.database, self.library))
        self.database.write_bytes(b"")  # a cable pulled mid-write
        error = db_rescue.unreadable(self.database, self.library)
        self.assertEqual(error, db_rescue.GONE)
        db_rescue.rescue(self.database, self.library, str(error))
        back = sqlite3.connect(str(self.database))
        self.assertEqual(back.execute("SELECT id FROM novels").fetchall(), [("a",)])
        back.close()

    def test_a_new_library_with_no_database_yet_is_not_a_damaged_one(self) -> None:
        self.assertIsNone(db_rescue.unreadable(self.database, self.library))

    def test_a_drive_that_only_cannot_be_reached_is_not_repaired(self) -> None:
        self.database.write_bytes(b"x" * 4096)
        busy = sqlite3.OperationalError("disk I/O error")
        with (
            patch.object(db_rescue.sqlite3, "connect", side_effect=busy),
            patch.object(db_rescue.time, "sleep"),
        ):
            self.assertIsNone(db_rescue.unreadable(self.database, self.library))

    def test_a_file_that_is_not_a_database_still_is(self) -> None:
        self.database.write_bytes(b"not a database at all " * 400)
        self.assertIn("not a database", db_rescue.unreadable(self.database, self.library) or "")


if __name__ == "__main__":
    unittest.main()
