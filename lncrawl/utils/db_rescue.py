"""Getting a library's database back when it cannot be read.

A desktop library's database lives on whatever drive the library is on, often
an external one, and a drive that loses power or is pulled out mid-write can
leave the first block of the file damaged. SQLite then refuses the whole file
("file is not a database") and the app would not start, though every page of
the library past that first block may be intact.

What is done here, in order, and always on copies:

1. The file's first block is looked at. If only the 100-byte header is damaged
   and the first table page is still there, a new header is written for a copy
   (its page size taken from the journal, or from what the first page says),
   and the copy is rebuilt into a clean database with SQLite's own VACUUM INTO.
2. Else the newest automatic copy of the database (``backups/database``) that
   reads cleanly is used.

What was found and done is written to ``database-rescue.txt`` in the library's
logs. The unreadable file and its journal are kept beside it, renamed, and
never deleted. Only the standard library is used: this runs before the rest of
the app can.
"""

import os
from pathlib import Path
import shutil
import sqlite3
import struct
import time
from typing import Callable, List, Optional, Tuple

MAGIC = b"SQLite format 3\x00"
PAGE_SIZES = (4096, 1024, 2048, 8192, 16384, 32768, 65536, 512)
SNAPSHOTS = Path("backups") / "database"
REPORT = Path("logs") / "database-rescue.txt"

Say = Callable[[str], None]


class DatabaseUnreadable(Exception):
    """The database cannot be read, and could not be got back. Nothing was changed."""


def unreadable(database: Path) -> Optional[str]:
    """Why ``database`` cannot be opened, or None when it can (or is not there yet)."""
    if not database.is_file() or database.stat().st_size == 0:
        return None
    try:
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error as error:
        return str(error)
    try:
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return None
    except sqlite3.DatabaseError as error:
        return str(error)
    finally:
        connection.close()


def _journal_page_size(database: Path) -> Optional[int]:
    """The page size the database's write-ahead journal was written with, if it has one."""
    try:
        with open(f"{database}-wal", "rb") as wal:
            head = wal.read(32)
    except OSError:
        return None
    if len(head) < 32 or struct.unpack(">I", head[:4])[0] not in (0x377F0682, 0x377F0683):
        return None
    size = struct.unpack(">I", head[8:12])[0]
    return size if size in PAGE_SIZES else None


def _plausible_first_page(head: bytes, page_size: int, file_size: int) -> bool:
    """Whether ``head`` holds a table page at offset 100 for this page size."""
    if file_size % page_size or len(head) < 112:
        return False
    kind = head[100]
    if kind not in (0x0D, 0x05):  # a leaf or interior table page: the schema's
        return False
    cells = struct.unpack(">H", head[103:105])[0]
    start = struct.unpack(">H", head[105:107])[0] or 65536
    if start > page_size or cells > page_size // 4:
        return False
    if kind == 0x05:
        right = struct.unpack(">I", head[108:112])[0]
        if not 1 < right <= file_size // page_size:
            return False
    return True


def _header(page_size: int, pages: int, wal: bool) -> bytes:
    """A database header for a file of this many pages of this size."""
    header = bytearray(100)
    header[0:16] = MAGIC
    header[16:18] = struct.pack(">H", 1 if page_size == 65536 else page_size)
    header[18] = header[19] = 2 if wal else 1  # write-ahead journal, or not
    header[20] = 0  # no space kept at the end of each page
    header[21:24] = bytes((64, 32, 32))
    header[24:28] = struct.pack(">I", 1)  # change counter
    header[28:32] = struct.pack(">I", pages)
    header[32:40] = bytes(8)  # the list of free pages is lost; VACUUM INTO makes a new one
    header[40:44] = struct.pack(">I", 1)  # schema cookie
    header[44:48] = struct.pack(">I", 4)  # schema format
    header[56:60] = struct.pack(">I", 1)  # UTF-8
    header[92:96] = struct.pack(">I", 1)  # the page count is valid for change counter 1
    header[96:100] = struct.pack(">I", sqlite3.sqlite_version_info[0] * 1000000)
    return bytes(header)


def _clean_copy(source: Path, clean: Path, say: Say) -> bool:
    """Rebuild ``source`` (with its journal, if any) into ``clean``; whether it is whole."""
    clean.unlink(missing_ok=True)
    try:
        connection = sqlite3.connect(str(source), timeout=30)
        try:
            check = connection.execute("PRAGMA quick_check(5)").fetchall()
            say(f"  check of the repaired copy: {'; '.join(str(row[0]) for row in check)}")
            connection.execute("VACUUM INTO ?", (str(clean),))
        finally:
            connection.close()
        connection = sqlite3.connect(str(clean))
        try:
            verdict = connection.execute("PRAGMA integrity_check(5)").fetchall()
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        finally:
            connection.close()
    except sqlite3.Error as error:
        say(f"  could not be rebuilt: {error}")
        clean.unlink(missing_ok=True)
        return False
    whole = [row[0] for row in verdict] == ["ok"] and "novels" in tables
    say(
        f"  rebuilt copy: {'whole' if whole else 'not whole: ' + str(verdict)} ({len(tables)} tables)"
    )
    if not whole:
        clean.unlink(missing_ok=True)
    return whole


def _repair_header(database: Path, work: Path, say: Say) -> Optional[Path]:
    """A clean database from ``database`` with a new header, or None if it cannot be."""
    size = database.stat().st_size
    with open(database, "rb") as source:
        head = source.read(65536 + 112)
    say(f"File size: {size:,} bytes. First 32 bytes: {head[:32].hex(' ')}")
    if head[:16] != MAGIC:
        leading = len(head) - len(head.lstrip(b"\x00"))
        say(f"The file does not start as a database ({leading} zero bytes at its start).")
    sizes: List[int] = []
    journal = _journal_page_size(database)
    if journal:
        say(f"Its journal was written with {journal}-byte pages.")
        sizes.append(journal)
    if head[:16] == MAGIC:
        stated = struct.unpack(">H", head[16:18])[0]
        stated = 65536 if stated == 1 else stated
        if stated in PAGE_SIZES:
            sizes.append(stated)
    sizes += [s for s in PAGE_SIZES if s not in sizes]
    candidates = [s for s in sizes if _plausible_first_page(head, s, size)]
    if not candidates:
        say(
            "Its first table page is damaged too, so a new header cannot bring it back. "
            "(The rest of the file may still hold the library: keep it.)"
        )
        return None
    wal = Path(f"{database}-wal")
    for page_size in candidates:
        say(f"Trying a new header for {page_size}-byte pages...")
        copy = work / "repaired.db"
        shutil.copyfile(database, copy)
        with open(copy, "r+b") as patched:
            patched.write(_header(page_size, size // page_size, wal.is_file()))
        Path(f"{copy}-wal").unlink(missing_ok=True)
        Path(f"{copy}-shm").unlink(missing_ok=True)
        if wal.is_file():
            shutil.copyfile(wal, f"{copy}-wal")  # the latest changes are in it
        clean = work / "clean.db"
        if _clean_copy(copy, clean, say):
            return clean
        if wal.is_file():
            say("  trying again without its journal (the newest changes would be lost)")
            Path(f"{copy}-wal").unlink(missing_ok=True)
            Path(f"{copy}-shm").unlink(missing_ok=True)
            if _clean_copy(copy, clean, say):
                return clean
    return None


def snapshots(library: Path) -> List[Path]:
    """The automatic copies of the database, newest first."""
    folder = library / SNAPSHOTS
    if not folder.is_dir():
        return []
    return sorted(folder.glob("sqlite-*.db"), key=lambda file: file.name, reverse=True)


def _from_snapshot(library: Path, work: Path, say: Say) -> Optional[Tuple[Path, Path]]:
    for snapshot in snapshots(library):
        say(f"Trying the automatic copy {snapshot.name}...")
        copy = work / "snapshot.db"
        shutil.copyfile(snapshot, copy)
        clean = work / "clean.db"
        if _clean_copy(copy, clean, say):
            return clean, snapshot
    return None


def rescue(database: Path, library: Path, error: str) -> str:
    """Put a readable database in place of ``database``; what was done, for the person.

    Raises `DatabaseUnreadable`, with what was found, when it cannot be done.
    The unreadable file and its journal are kept, renamed, either way.
    """
    lines: List[str] = []
    report = library / REPORT

    def say(text: str) -> None:
        lines.append(text)

    def write_report() -> None:
        try:
            report.parent.mkdir(parents=True, exist_ok=True)
            with open(report, "a", encoding="utf-8") as out:
                out.write("\n".join(lines) + "\n\n")
        except OSError:
            pass

    stamp = time.strftime("%Y%m%d-%H%M%S")
    say(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')}: {database} cannot be read: {error}")
    size = database.stat().st_size
    wal = Path(f"{database}-wal")
    wal_size = wal.stat().st_size if wal.is_file() else 0
    say(f"Its journal ({wal.name}): {f'{wal_size:,} bytes' if wal.is_file() else 'none'}")
    try:
        free = shutil.disk_usage(library).free
    except OSError:
        free = 0
    need = 2 * (size + wal_size) + 64 * 1024 * 1024
    if free < need:
        say(f"Not enough room to work on a copy: {free:,} bytes free, {need:,} needed.")
        write_report()
        raise DatabaseUnreadable(
            f"The library's database ({database}) cannot be read ({error}), and there is not "
            f"enough room on its drive to try to repair a copy. Free up about "
            f"{need // (1024 * 1024)} MB there and start again. Nothing was changed. "
            f"What was found is in {report}"
        )
    work = library / "tmp" / f"rescue-{stamp}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        found = _repair_header(database, work, say)
        source = "a repaired copy of itself"
        if found is None:
            from_snapshot = _from_snapshot(library, work, say)
            if from_snapshot is not None:
                found, snapshot = from_snapshot
                source = f"the automatic copy made {snapshot.stem[len('sqlite-') :]}"
        if found is None:
            say("Nothing could be got back. The file was not changed.")
            write_report()
            raise DatabaseUnreadable(
                f"The library's database ({database}) cannot be read ({error}), and it could "
                f"not be repaired. Nothing was changed or deleted: your novels' files are all "
                f"still in {library}. What was found is in {report}"
            )
        # the unreadable file and its journal are kept, renamed
        kept = []
        for suffix in ("", "-wal", "-shm"):
            old = Path(f"{database}{suffix}")
            if old.is_file():
                aside = old.with_name(f"{database.name}.unreadable-{stamp}{suffix}")
                os.replace(old, aside)
                kept.append(aside.name)
        os.replace(found, database)
        said = (
            f"The library's database could not be read ({error}), so it was replaced by "
            f"{source}. The unreadable file was kept as {', '.join(kept)} in {library}."
        )
        if not source.startswith("a repaired"):
            said += " Anything added since that copy was made is not in it."
        say(said)
        write_report()
        return said
    finally:
        shutil.rmtree(work, ignore_errors=True)


def take_snapshot(database: Path, library: Path, keep: int = 2) -> Optional[Path]:
    """A consistent copy of ``database`` in ``backups/database``, keeping the newest ``keep``.

    None, and nothing written, when there is not room for it.
    """
    folder = library / SNAPSHOTS
    folder.mkdir(parents=True, exist_ok=True)
    size = database.stat().st_size
    wal = Path(f"{database}-wal")
    size += wal.stat().st_size if wal.is_file() else 0
    if shutil.disk_usage(folder).free < 2 * size + 1024 * 1024 * 1024:
        return None
    target = folder / f"sqlite-{time.strftime('%Y%m%d-%H%M%S')}.db"
    writing = target.with_name(f"{target.name}.part")
    writing.unlink(missing_ok=True)
    source = sqlite3.connect(str(database), timeout=60)
    try:
        copy = sqlite3.connect(str(writing))
        try:
            with copy:
                source.backup(copy)
        finally:
            copy.close()
    finally:
        source.close()
    os.replace(writing, target)
    for old in snapshots(library)[keep:]:
        old.unlink(missing_ok=True)
    return target
