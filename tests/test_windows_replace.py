"""Files Windows will not let be replaced for a moment, and one import per ZIP."""

from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Iterator
import unittest
from unittest.mock import patch

from sqlmodel import Session, SQLModel, create_engine

from lncrawl import relocation
from lncrawl.dao import Job, User
from lncrawl.enums import JobStatus, JobType
from lncrawl.services.jobs import service as jobs_module
from lncrawl.utils import file_tools


class ReplaceTests(unittest.TestCase):
    def test_a_file_held_for_a_moment_is_written_when_it_is_let_go(self) -> None:
        with TemporaryDirectory() as temp:
            target = Path(temp) / "cover.jpg"
            target.write_bytes(b"old")
            real = file_tools.os.replace
            calls = []

            def held_once(source, path):
                calls.append(1)
                if len(calls) == 1:
                    raise PermissionError(5, "Access is denied")
                return real(source, path)

            with (
                patch.object(file_tools.os, "replace", held_once),
                patch.object(file_tools, "_WINDOWS", True),
                patch.object(file_tools.os, "chmod"),
                patch.object(file_tools, "REPLACE_WAIT", 0),
            ):
                with file_tools.atomic_write(target) as out:
                    out.write(b"new")
            self.assertEqual(target.read_bytes(), b"new")
            self.assertEqual(len(calls), 2)

    def test_a_file_that_stays_held_still_fails(self) -> None:
        with TemporaryDirectory() as temp:
            target = Path(temp) / "cover.jpg"
            denied = PermissionError(5, "Access is denied")
            with (
                patch.object(file_tools.os, "replace", side_effect=denied),
                patch.object(file_tools, "_WINDOWS", True),
                patch.object(file_tools.os, "chmod"),
                patch.object(file_tools, "REPLACE_WAIT", 0),
            ):
                with self.assertRaises(PermissionError):
                    with file_tools.atomic_write(target) as out:
                        out.write(b"new")
            self.assertEqual(list(Path(temp).iterdir()), [])  # no temp file left


class OneImportPerZipTests(unittest.TestCase):
    def test_the_same_zip_is_not_imported_twice_at_once(self) -> None:
        engine = create_engine("sqlite://")
        SQLModel.metadata.create_all(engine)

        @contextmanager
        def session() -> Iterator[Session]:
            with Session(engine) as sess:
                yield sess

        user = User(id="u", email="u@example.com", password="x")
        with Session(engine) as sess:
            sess.add(user)
            sess.add(
                Job(
                    id="first",
                    user_id="u",
                    type=JobType.IMPORT_ARCHIVE,
                    status=JobStatus.RUNNING,
                    extra={"archive_file": "F:\\\\Novels\\\\library.zip"},
                )
            )
            sess.commit()
        fake = SimpleNamespace(db=SimpleNamespace(session=session))
        with patch.object(jobs_module, "ctx", fake):
            service = jobs_module.JobService.__new__(jobs_module.JobService)
            job = service.import_archive(user, "F:\\\\Novels\\\\library.zip", "library.zip")
        self.assertEqual(job.id, "first")


class SplitLeavesInUseFilesTests(unittest.TestCase):
    def test_a_file_in_use_is_left_behind_instead_of_ending_the_split(self) -> None:
        with TemporaryDirectory() as temp:
            root = Path(temp)
            base, drive, here = root / "base", root / "drive", root / "here"
            base.mkdir()
            (drive / "novels" / "a").mkdir(parents=True)
            (drive / "app-browser").mkdir()
            (drive / "app-browser" / "Cookies").write_text("locked")
            (drive / "config.json").write_text("{}")
            import sqlite3

            database = sqlite3.connect(str(drive / "sqlite.db"))
            database.execute("CREATE TABLE novels (id TEXT)")
            database.commit()
            database.close()
            real = relocation.shutil.copy2

            def locked(source, target, *args, **kwargs):
                if str(source).endswith("Cookies"):
                    raise PermissionError(32, "in use by another process")
                return real(source, target, *args, **kwargs)

            with patch.object(relocation.shutil, "copy2", locked):
                relocation.split(base, drive, here)
            self.assertTrue((here / "config.json").is_file())
            self.assertFalse((here / "app-browser" / "Cookies").exists())
            self.assertIn("Left behind, in use", (drive / relocation.MOVE_LOG).read_text())


if __name__ == "__main__":
    unittest.main()
