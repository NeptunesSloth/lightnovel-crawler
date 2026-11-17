"""Moving the data folder to another drive (LNCRAWL_DATA_PATH) keeps everything.

The library outgrows the system drive: hundreds of gigabytes of chapters and
manga pages. Moving the whole folder and pointing LNCRAWL_DATA_PATH at the new
place has to find the same database and the same finished exports there.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from lncrawl.services.scheduler.handlers import export_source

ROOT = Path(__file__).resolve().parents[1]

FIRST_RUN = """
from lncrawl.context import ctx
ctx.setup(sync_remote_index=False)
from lncrawl.dao import Novel
with ctx.db.session() as session:
    session.add(Novel(id="moved", url="https://example.com/moved", domain="example.com",
                      title="Moved Novel"))
    session.commit()
ctx.config.crawler.disk_size_limit = 0  # any setting saved writes config.json
ctx.destroy()
"""

AFTER_MOVE = """
from lncrawl.context import ctx
ctx.setup(sync_remote_index=False)
from lncrawl.dao import Novel
with ctx.db.session() as session:
    novel = session.get(Novel, "moved")
print("NOVEL", novel.title if novel else None)
print("DATABASE", ctx.config.db.url)
ctx.destroy()
"""


def run_app(code: str, data: Path) -> str:
    env = dict(os.environ, LNCRAWL_DATA_PATH=str(data))
    env.pop("DATABASE_URL", None)
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise AssertionError((result.stdout + result.stderr)[-4000:])
    return result.stdout


class MovedDataFolderTests(unittest.TestCase):
    def test_the_database_moves_with_the_data_folder(self) -> None:
        with TemporaryDirectory() as temp:
            old, new = Path(temp) / "old" / "LNCrawl", Path(temp) / "ssd" / "LNCrawl"
            run_app(FIRST_RUN, old)
            saved = json.loads((old / "config.json").read_text(encoding="utf-8"))
            # what made the move lose the library: the old folder written down
            self.assertIn(old.as_posix(), saved["database"]["url"])

            new.parent.mkdir(parents=True)
            shutil.move(str(old), str(new))
            output = run_app(AFTER_MOVE, new)

            self.assertIn("NOVEL Moved Novel", output)
            self.assertIn(f"DATABASE sqlite:///{new.resolve().as_posix()}/sqlite.db", output)
            self.assertFalse(old.exists(), "nothing may be written to the old folder")

    def test_a_database_named_on_purpose_is_left_alone(self) -> None:
        from lncrawl import config

        with TemporaryDirectory() as temp:
            data, elsewhere = Path(temp) / "data", Path(temp) / "elsewhere"
            data.mkdir()
            (data / "sqlite.db").touch()
            chosen = f"sqlite:///{(elsewhere / 'library.db').as_posix()}"
            settings = config.Config()
            settings._data = {"database": {"url": chosen}}
            with patch.object(config, "APP_DIR", data):
                self.assertEqual(settings.db.url, chosen)


class MovedExportRecordsTests(unittest.TestCase):
    def test_finished_exports_are_found_in_the_moved_folder(self) -> None:
        # The resume record names each finished e-book by its full path; after
        # the move every novel looked unfinished and was gone over again.
        with TemporaryDirectory() as temp:
            data = Path(temp) / "ssd" / "LNCrawl"
            book = data / "novels" / "abc" / "artifacts" / "Moved Novel.epub"
            book.parent.mkdir(parents=True)
            book.write_bytes(b"epub")
            manifest = data / "exports" / "manifest" / "example.com.json"
            manifest.parent.mkdir(parents=True)
            archive = data / "exports" / "job.zip"
            archive.write_bytes(b"zip")
            old = "C:\\Users\\reader\\AppData\\Roaming\\LNCrawl"
            manifest.write_text(
                json.dumps(
                    {
                        "novels": {
                            "https://example.com/moved": {
                                "file": f"{old}\\novels\\abc\\artifacts\\Moved Novel.epub",
                                "archive": f"{old}\\exports\\job.zip",
                                "missing": 0,
                            },
                            "https://example.com/elsewhere": {
                                "file": "D:\\Books\\Gone.epub",
                                "missing": 0,
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            fake = SimpleNamespace(files=SimpleNamespace(resolve=lambda path: data / path))
            with patch.object(export_source, "ctx", fake):
                records = export_source._load_manifest("example.com")

            moved = records["https://example.com/moved"]
            self.assertEqual(Path(moved["file"]), book)
            self.assertEqual(Path(moved["archive"]), archive)
            # a file that is not in the data folder is not guessed at
            self.assertEqual(
                records["https://example.com/elsewhere"]["file"], "D:\\Books\\Gone.epub"
            )


if __name__ == "__main__":
    unittest.main()
