"""A library's database that cannot be read is got back, not wiped.

Reported: a library on an external drive would not start, "file is not a
database", and the desktop app asked on every start for a database that failed
to be wiped and made again.
"""

import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lncrawl.utils import db_rescue
from lncrawl.utils.db_rescue import DatabaseUnreadable, rescue, take_snapshot, unreadable

ROOT = Path(__file__).resolve().parents[1]


def make_library(folder: Path, novels: int = 300) -> sqlite3.Connection:
    """A database like the app's: write-ahead journal, more than one page, a novels table.

    The connection is left open, with its newest change still only in the
    journal: closing it would fold the journal into the file.
    """
    folder.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(folder / "sqlite.db"))
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE novels (id TEXT PRIMARY KEY, title TEXT, synopsis TEXT)")
    with connection:
        for n in range(novels):
            connection.execute(
                "INSERT INTO novels VALUES (?, ?, ?)", (f"n{n}", f"Novel {n}", "words " * 50)
            )
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    with connection:
        connection.execute("INSERT INTO novels VALUES ('latest', 'Added Last', '')")
    return connection


def titles(database: Path) -> list:
    # read-only: a connection that may write folds the journal in when it closes
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        return [row[0] for row in connection.execute("SELECT id FROM novels ORDER BY rowid")]
    finally:
        connection.close()


def damage(database: Path, length: int, byte: bytes = b"\x00") -> None:
    with open(database, "r+b") as file:
        file.write(byte * length)


class RescueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.library = Path(self.temp.name) / "Novels"
        self.database = self.library / "sqlite.db"
        # a library whose newest change is still in its journal: copy the files
        # while the writer is open, as a drive losing power would leave them
        source = Path(self.temp.name) / "live"
        live = make_library(source)
        self.library.mkdir()
        for suffix in ("", "-wal"):
            Path(f"{self.database}{suffix}").write_bytes(
                Path(f"{source / 'sqlite.db'}{suffix}").read_bytes()
            )
        live.close()
        self.expected = [f"n{n}" for n in range(300)] + ["latest"]
        self.assertEqual(titles(self.database), self.expected)

    def test_a_damaged_header_is_repaired_with_the_newest_changes(self) -> None:
        damage(self.database, 100)
        error = unreadable(self.database)
        self.assertIn("not a database", error or "")
        said = rescue(self.database, self.library, error or "")
        self.assertIn("repaired copy", said)
        self.assertIsNone(unreadable(self.database))
        self.assertEqual(titles(self.database), self.expected)  # the journal's change too
        kept = sorted(p.name for p in self.library.glob("sqlite.db.unreadable-*"))
        # the file and its journal, both kept as they were
        self.assertTrue(
            any(name.endswith(("0", "1", "2", "3", "4", "5", "6", "7", "8", "9")) for name in kept)
        )
        self.assertTrue(any(name.endswith("-wal") for name in kept))
        report = (self.library / db_rescue.REPORT).read_text()
        self.assertIn("4096-byte pages", report)
        self.assertFalse(any((self.library / "tmp").glob("rescue-*")))

    def test_a_header_overwritten_with_rubbish_is_repaired(self) -> None:
        damage(self.database, 64, b"\xab")
        rescue(self.database, self.library, "file is not a database")
        self.assertEqual(titles(self.database), self.expected)

    def test_without_a_journal_the_page_size_is_worked_out_from_the_file(self) -> None:
        connection = sqlite3.connect(str(self.database))
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.close()
        damage(self.database, 100)
        rescue(self.database, self.library, "file is not a database")
        self.assertEqual(titles(self.database), self.expected)

    def test_a_damaged_first_page_without_a_copy_is_left_as_it_was(self) -> None:
        damage(self.database, 4096)
        before = self.database.read_bytes()
        with self.assertRaises(DatabaseUnreadable) as caught:
            rescue(self.database, self.library, "file is not a database")
        self.assertIn("Nothing was changed or deleted", str(caught.exception))
        self.assertEqual(self.database.read_bytes(), before)
        self.assertTrue(Path(f"{self.database}-wal").is_file())
        self.assertIn("first table page is damaged", (self.library / db_rescue.REPORT).read_text())

    def test_a_damaged_first_page_is_replaced_by_the_newest_automatic_copy(self) -> None:
        older = take_snapshot(self.database, self.library)
        self.assertIsNotNone(older)
        damage(self.database, 4096)
        said = rescue(self.database, self.library, "file is not a database")
        self.assertIn("automatic copy", said)
        self.assertIn("Anything added since", said)
        self.assertEqual(titles(self.database), self.expected)

    def test_with_no_room_to_work_nothing_is_touched(self) -> None:
        damage(self.database, 100)
        before = self.database.read_bytes()
        full = SimpleNamespace(free=1024, total=10**12, used=0)
        with patch.object(db_rescue.shutil, "disk_usage", return_value=full):
            with self.assertRaisesRegex(DatabaseUnreadable, "not enough room"):
                rescue(self.database, self.library, "file is not a database")
        self.assertEqual(self.database.read_bytes(), before)

    def test_only_the_two_newest_automatic_copies_are_kept(self) -> None:
        made = []
        for stamp in ("20260101-000000", "20260102-000000", "20260103-000000"):
            with patch.object(db_rescue.time, "strftime", return_value=stamp):
                made.append(take_snapshot(self.database, self.library))
        self.assertEqual(
            [p.name for p in db_rescue.snapshots(self.library)],
            ["sqlite-20260103-000000.db", "sqlite-20260102-000000.db"],
        )
        self.assertEqual(titles(made[-1]), self.expected)


class NeverWipedTests(unittest.TestCase):
    """A start that fails part way never wipes a database with a library in it."""

    def setUp(self) -> None:
        from lncrawl.services import db as db_module

        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.database = self.folder / "sqlite.db"
        config = SimpleNamespace(
            db=SimpleNamespace(
                url=f"sqlite:///{self.database.as_posix()}",
                pool_size=5,
                pool_timeout=30,
                pool_recycle=3600,
                connect_timeout=5,
            ),
            app=SimpleNamespace(app_dir=self.folder),
        )
        ctx = SimpleNamespace(config=config, logger=SimpleNamespace(is_debug=False))
        patcher = patch.object(db_module, "ctx", ctx)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.db = db_module.DB()
        self.addCleanup(self.db.close)

    def failing_start(self) -> None:
        with patch.object(
            type(self.db), "base_revision", side_effect=RuntimeError("upgrade failed")
        ):
            self.db.bootstrap(reset_on_failure=True)

    def test_a_library_is_not_wiped_when_the_start_fails(self) -> None:
        connection = sqlite3.connect(str(self.database))
        connection.execute("CREATE TABLE novels (id TEXT)")
        with connection:
            connection.execute("INSERT INTO novels VALUES ('kept')")
        connection.close()
        with self.assertRaisesRegex(RuntimeError, "upgrade failed"):
            self.failing_start()
        self.assertEqual(titles(self.database), ["kept"])

    def test_an_empty_database_is_still_made_again(self) -> None:
        connection = sqlite3.connect(str(self.database))
        connection.execute("CREATE TABLE novels (id TEXT)")
        connection.close()
        resets = []
        with (
            patch.object(type(self.db), "_reset_database", lambda db: resets.append(1)),
            patch.object(type(self.db), "base_revision", side_effect=[RuntimeError("x"), None]),
            patch("alembic.command.upgrade"),
            patch.object(type(self.db), "_verify_schema"),
        ):
            self.db.bootstrap(reset_on_failure=True)
        self.assertEqual(resets, [1])


START = """
from lncrawl.context import ctx
ctx.setup(sync_remote_index=False, reset_db_on_failure=True)
from lncrawl.dao import Novel
with ctx.db.session() as session:
    print("NOVEL", session.get(Novel, "kept").title)
print("RESCUED", ctx.db.rescued)
ctx.destroy()
"""

FIRST = """
from lncrawl.context import ctx
ctx.setup(sync_remote_index=False)
from lncrawl.dao import Novel
with ctx.db.session() as session:
    session.add(Novel(id="kept", url="https://example.com/kept", domain="example.com",
                      title="Kept Novel"))
    session.commit()
ctx.destroy()
"""


class RealAppTests(unittest.TestCase):
    def run_app(self, code: str, data: Path) -> subprocess.CompletedProcess:
        env = dict(os.environ, LNCRAWL_DATA_PATH=str(data))
        env.pop("DATABASE_URL", None)
        return subprocess.run(
            [sys.executable, "-c", code],
            env=env,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )

    def test_the_app_starts_with_its_library_after_the_header_was_damaged(self) -> None:
        with TemporaryDirectory() as temp:
            data = Path(temp) / "Novels"
            self.assertEqual(self.run_app(FIRST, data).returncode, 0)
            damage(data / "sqlite.db", 100)
            result = self.run_app(START, data)
            self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-3000:])
            self.assertIn("NOVEL Kept Novel", result.stdout)
            self.assertIn("RESCUED The library's database could not be read", result.stdout)

    def test_an_app_that_cannot_get_it_back_stops_with_the_reason_and_wipes_nothing(self) -> None:
        with TemporaryDirectory() as temp:
            data = Path(temp) / "Novels"
            self.assertEqual(self.run_app(FIRST, data).returncode, 0)
            damage(data / "sqlite.db", 4096)
            before = (data / "sqlite.db").read_bytes()
            result = self.run_app(START, data)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("could not be repaired", result.stderr)
            self.assertEqual((data / "sqlite.db").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
