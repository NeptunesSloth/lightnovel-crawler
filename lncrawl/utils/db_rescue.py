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
2. Else, if the first page itself is damaged, the newest whole copy of it in
   the write-ahead journal is put in its place, on a copy, and rebuilt the same
   way: the first page holds the list of tables, and is written to the journal
   whenever the database grows.
3. Else the newest automatic copy of the database (``backups/database``) that
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
from typing import Callable, Iterator, List, Optional, Tuple

MAGIC = b"SQLite format 3\x00"
PAGE_SIZES = (4096, 1024, 2048, 8192, 16384, 32768, 65536, 512)
SNAPSHOTS = Path("backups") / "database"
REPORT = Path("logs") / "database-rescue.txt"

Say = Callable[[str], None]


class DatabaseUnreadable(Exception):
    """The database cannot be read, and could not be got back. Nothing was changed."""


# What SQLite says of a file that is damaged. Anything else ("disk I/O error"
# from a drive still waking, "database is locked" by another copy of the app)
# says nothing about the file: a repair then copied a gigabyte, rebuilt it and
# swapped it in for a database that was healthy.
_DAMAGED = ("not a database", "malformed", "disk image")
GONE = "the database file is missing or empty, and the library has automatic copies of it"


def unreadable(database: Path, library: Optional[Path] = None) -> Optional[str]:
    """Why ``database`` cannot be read and should be got back, or None.

    None when it opens, when it is not there yet, and when it only cannot be
    reached just now. With ``library``, a database that is missing or empty
    while the library holds automatic copies of it counts as unreadable: a
    cable pulled mid-write can leave it at 0 bytes, and starting from an empty
    one showed an empty library and soon rotated the good copies away.
    """
    if not database.is_file() or database.stat().st_size == 0:
        if library is not None and all_snapshots(library):
            return GONE
        return None
    for attempt in range(4):
        try:
            connection = sqlite3.connect(
                f"file:{database.as_posix()}?mode=ro", uri=True, timeout=30
            )
            try:
                connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
                return None
            finally:
                connection.close()
        except sqlite3.Error as error:
            if any(sign in str(error).lower() for sign in _DAMAGED):
                return str(error)
            time.sleep(2 * (attempt + 1))  # the drive may be waking: look again
    return None


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


def _journal_frames(wal: Path, page_size: int) -> Iterator[Tuple[int, int, bytes]]:
    """Every frame in a write-ahead journal: (order, page number, page), whatever its age.

    Frames left from before the journal last started over are still in the
    file past the current ones, and still hold pages as they once were.
    """
    frame = 24 + page_size
    with open(wal, "rb") as journal:
        journal.seek(32)
        order = 0
        while True:
            block = journal.read(frame)
            if len(block) < frame:
                return
            page_number = struct.unpack(">I", block[:4])[0]
            yield order, page_number, block[24:]
            order += 1


def _page_one_from_journal(database: Path, work: Path, say: Say) -> Optional[Path]:
    """A clean database from ``database`` with its first page taken from its journal.

    The first page holds the list of tables, and is written to the journal
    whenever the database grows: on a library downloading all day, there are
    copies of it there from the last minutes.
    """
    wal = Path(f"{database}-wal")
    page_size = _journal_page_size(database)
    if page_size is None:
        say("There is no journal to take its first page from.")
        return None
    size = database.stat().st_size
    pages = [
        (order, page)
        for order, number, page in _journal_frames(wal, page_size)
        if number == 1 and page[:16] == MAGIC and page[100] in (0x0D, 0x05)
    ]
    say(f"Its journal holds {len(pages)} cop(ies) of its first page.")
    newest = sorted(pages, reverse=True)[:5]
    for tried, (order, page) in enumerate(newest, start=1):
        say(f"Trying the first page from journal frame {order} ({tried} of {len(newest)})...")
        first = bytearray(page)
        # the page count and free list it held were for the file as it was then
        first[28:32] = struct.pack(">I", max(size, len(first)) // page_size)
        first[32:40] = bytes(8)
        first[92:96] = first[24:28]
        copy = work / "from-journal.db"
        shutil.copyfile(database, copy)
        with open(copy, "r+b") as patched:
            patched.write(bytes(first))
        for keep_journal in (True, False):
            Path(f"{copy}-wal").unlink(missing_ok=True)
            Path(f"{copy}-shm").unlink(missing_ok=True)
            if keep_journal:
                shutil.copyfile(wal, f"{copy}-wal")
            else:
                say("  trying again without the journal's newest changes")
            clean = work / "clean.db"
            if _clean_copy(copy, clean, say):
                return clean
    return None


# ---------------------------------------------------------------------------- #
# reading the tables page by page, when the first page is gone
# ---------------------------------------------------------------------------- #

# Set while a rescue runs: it can take minutes on a big library, and the
# desktop app waits for it instead of taking the wait for a failed start.
busy = False

_SCHEMA_KINDS = ("table", "index", "view", "trigger")


def _varint(data: bytes, at: int) -> Tuple[int, int]:
    """SQLite's variable-length integer at ``at``: (value, where the next thing starts)."""
    value = 0
    for i in range(8):
        byte = data[at + i]
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, at + i + 1
    return (value << 8) | data[at + 8], at + 9


def _record(payload: bytes) -> List[object]:
    """The values of one row, as the file holds them."""
    header_size, at = _varint(payload, 0)
    kinds: List[int] = []
    while at < header_size:
        kind, at = _varint(payload, at)
        kinds.append(kind)
    values: List[object] = []
    at = header_size
    for kind in kinds:
        if kind == 0:
            values.append(None)
        elif 1 <= kind <= 6:
            width = (1, 2, 3, 4, 6, 8)[kind - 1]
            values.append(int.from_bytes(payload[at : at + width], "big", signed=True))
            at += width
        elif kind == 7:
            values.append(struct.unpack(">d", payload[at : at + 8])[0])
            at += 8
        elif kind in (8, 9):
            values.append(kind - 8)
        elif kind >= 12:
            width = (kind - 12) // 2
            raw = payload[at : at + width]
            values.append(raw.decode("utf-8", "replace") if kind % 2 else bytes(raw))
            at += width
        else:
            raise ValueError(f"no such kind of value: {kind}")
    if at > len(payload):
        raise ValueError("the row runs past its end")
    return values


class _Pages:
    """A database file read as pages, whatever its first page says."""

    def __init__(self, path: Path, page_size: int) -> None:
        self.file = open(path, "rb")
        self.size = page_size
        self.count = path.stat().st_size // page_size

    def close(self) -> None:
        self.file.close()

    def page(self, number: int) -> bytes:
        if not 1 <= number <= self.count:
            raise ValueError(f"page {number} is outside the file")
        self.file.seek((number - 1) * self.size)
        return self.file.read(self.size)

    def _payload(self, data: bytes, at: int, total: int) -> bytes:
        """A cell's payload, with what spilled to overflow pages."""
        usable = self.size
        most = usable - 35
        if total <= most:
            return data[at : at + total]
        least = ((usable - 12) * 32 // 255) - 23
        local = least + (total - least) % (usable - 4)
        if local > most:
            local = least
        parts = [data[at : at + local]]
        left = total - local
        following = struct.unpack(">I", data[at + local : at + local + 4])[0]
        seen = set()
        while left > 0 and following and following not in seen:
            seen.add(following)
            page = self.page(following)
            following = struct.unpack(">I", page[:4])[0]
            take = min(left, usable - 4)
            parts.append(page[4 : 4 + take])
            left -= take
        if left > 0:
            raise ValueError("an overflow chain ends early")
        return b"".join(parts)

    def cells(self, number: int) -> Tuple[int, List[int], List[Tuple[int, bytes]]]:
        """A table page: (kind, its children, its rows as (rowid, payload))."""
        data = self.page(number)
        kind = data[0]
        count = struct.unpack(">H", data[3:5])[0]
        children: List[int] = []
        rows: List[Tuple[int, bytes]] = []
        start = 12 if kind == 0x05 else 8
        if kind == 0x05:
            children.append(struct.unpack(">I", data[8:12])[0])
        for i in range(count):
            at = struct.unpack(">H", data[start + 2 * i : start + 2 * i + 2])[0]
            if kind == 0x05:
                children.append(struct.unpack(">I", data[at : at + 4])[0])
            elif kind == 0x0D:
                total, at = _varint(data, at)
                rowid, at = _varint(data, at)
                rows.append((rowid, self._payload(data, at, total)))
        return kind, children, rows

    def rows(self, root: int) -> Iterator[Tuple[int, List[object]]]:
        """Every row of the table whose tree starts at ``root``."""
        pending, seen = [root], set()
        while pending:
            number = pending.pop()
            if number in seen:
                continue
            seen.add(number)
            kind, children, rows = self.cells(number)
            if kind not in (0x05, 0x0D):
                raise ValueError(f"page {number} is not part of a table")
            pending.extend(reversed(children))
            for rowid, payload in rows:
                yield rowid, _record(payload)


def _guess_page_size(database: Path) -> Optional[int]:
    """The page size most pages of the file make sense at."""
    size = database.stat().st_size
    best, best_score = None, 0
    with open(database, "rb") as file:
        for page_size in PAGE_SIZES:
            if size % page_size:
                continue
            pages = size // page_size
            score = 0
            for number in range(2, min(pages, 400) + 1):
                file.seek((number - 1) * page_size)
                head = file.read(8)
                if head[:1] in (b"\x0d", b"\x05", b"\x0a", b"\x02"):
                    start = struct.unpack(">H", head[5:7])[0] or 65536
                    if start <= page_size:
                        score += 1
            if score > best_score:
                best, best_score = page_size, score
    return best


def _salvage(database: Path, work: Path, say: Say) -> Optional[Path]:
    """A new database with every table read straight from the pages of ``database``.

    For a first page that is gone: in a database the app made, it holds only
    pointers to the pages listing the tables, and those are still in the file.
    They are found by what their rows look like, and give each table's layout
    and where it starts; every row is then read and written to a new file.
    """
    page_size = _journal_page_size(database) or _guess_page_size(database)
    if page_size is None:
        say("No page size makes sense of the file: it cannot be read page by page.")
        return None
    pages = _Pages(database, page_size)
    try:
        say(f"Reading it page by page ({pages.count:,} pages of {page_size} bytes)...")
        schema: dict = {}
        for number in range(2, pages.count + 1):
            try:
                kind, _, rows = pages.cells(number)
            except Exception:
                continue
            if kind != 0x0D or not rows:
                continue
            entries = []
            for _, payload in rows:
                try:
                    values = _record(payload)
                except Exception:
                    entries = []
                    break
                if (
                    len(values) != 5
                    or values[0] not in _SCHEMA_KINDS
                    or not isinstance(values[3], int)
                ):
                    entries = []
                    break
                entries.append(values)
            for entry in entries:
                schema.setdefault(entry[1], []).append(entry)
        tables = {
            name: e
            for name, e in schema.items()
            if e[0][0] == "table" and not name.startswith("sqlite_")
        }
        say(
            f"Found the layout of {len(tables)} table(s) and {len(schema) - len(tables)} other thing(s)."
        )
        if "novels" not in tables:
            say("The novels table's layout was not found: nothing to rebuild from.")
            return None
        salvaged = work / "salvaged.db"
        salvaged.unlink(missing_ok=True)
        out = sqlite3.connect(str(salvaged))
        try:
            later = []
            for name, entries in schema.items():
                kind, _, _, _, sql = entries[-1]
                if not sql or name.startswith("sqlite_"):
                    continue
                if kind == "table":
                    out.execute(sql)
                else:
                    later.append((name, sql))
            for name, sql in later:  # indexes too, before the rows: duplicates are dropped
                try:
                    out.execute(sql)
                except sqlite3.Error as error:
                    say(f"  could not make {name}: {error}")
            lost = 0
            for name, entries in sorted(tables.items()):
                columns = [row[1] for row in out.execute(f'PRAGMA table_info("{name}")')]
                roots = []
                for entry in entries:  # stale copies of the list can name old pages
                    root = entry[3]
                    try:
                        roots.append((sum(1 for _ in pages.rows(root)), root))
                    except Exception as error:
                        say(f"  {name}: the copy starting at page {root} cannot be read ({error})")
                if not roots:
                    continue
                total, root = max(roots)
                added = 0
                batch: List[Tuple[str, List[object]]] = []

                def flush() -> int:
                    done = 0
                    for statement, values in batch:
                        try:
                            done += out.execute(statement, values).rowcount
                        except sqlite3.Error:
                            pass
                    batch.clear()
                    return done

                for rowid, values in pages.rows(root):
                    values = values[: len(columns)]
                    names = ", ".join(['"rowid"'] + [f'"{c}"' for c in columns[: len(values)]])
                    marks = ", ".join("?" * (len(values) + 1))
                    batch.append(
                        (
                            f'INSERT OR IGNORE INTO "{name}" ({names}) VALUES ({marks})',
                            [rowid, *values],
                        )
                    )
                    if len(batch) >= 5000:
                        added += flush()
                added += flush()
                lost += total - added
                say(f"  {name}: {added:,} row(s)" + (f" of {total:,}" if added != total else ""))
            out.commit()
            if lost:
                say(
                    f"{lost:,} row(s) could not be written back (unreadable, or the same row twice)."
                )
        finally:
            out.close()
        clean = work / "clean.db"
        return clean if _clean_copy(salvaged, clean, say) else None
    finally:
        pages.close()


def snapshots(library: Path) -> List[Path]:
    """The automatic copies of the database, newest first."""
    folder = library / SNAPSHOTS
    if not folder.is_dir():
        return []
    return sorted(folder.glob("sqlite-*.db"), key=lambda file: file.name, reverse=True)


# Other folders holding automatic copies (see `keep_elsewhere`), set by the app.
also_look_in: List[Path] = []


def all_snapshots(library: Path) -> List[Path]:
    """The automatic copies in the library and kept elsewhere, newest first."""
    found = list(snapshots(library))
    for folder in also_look_in:
        if folder != library:
            found.extend(snapshots(folder))
    return sorted(found, key=lambda file: file.name, reverse=True)


def _from_snapshot(library: Path, work: Path, say: Say) -> Optional[Tuple[Path, Path]]:
    for snapshot in all_snapshots(library):
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

    global busy
    busy = True
    try:
        return _rescue(database, library, error, lines, say, write_report, report)
    finally:
        busy = False


def _rescue(
    database: Path,
    library: Path,
    error: str,
    lines: List[str],
    say: Say,
    write_report: Callable[[], None],
    report: Path,
) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    say(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')}: {database} cannot be read: {error}")
    present = database.is_file() and database.stat().st_size > 0
    size = database.stat().st_size if present else 0
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
        found = _repair_header(database, work, say) if present else None
        source = "a repaired copy of itself"
        if found is None and present:
            found = _page_one_from_journal(database, work, say)
        if found is None and present:
            found = _salvage(database, work, say)
            if found is not None:
                source = "a copy rebuilt from what was left in it"
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
        if source.startswith("the automatic copy"):
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
                # in steps, letting writers through: a gigabyte in one step
                # held the journal open for minutes while downloads committed
                source.backup(copy, pages=2048, sleep=0.05)
        finally:
            copy.close()
    finally:
        source.close()
    os.replace(writing, target)
    for old in snapshots(library)[keep:]:
        old.unlink(missing_ok=True)
    return target


def keep_elsewhere(snapshot: Path, folder: Path, keep: int = 1) -> Optional[Path]:
    """Keep a copy of a snapshot in ``folder`` on another drive; None when it has no room.

    The copies in the library are on the drive whose failure they are for. One
    more, where the app starts from, survives that drive.
    """
    target_dir = folder / SNAPSHOTS
    target_dir.mkdir(parents=True, exist_ok=True)
    size = snapshot.stat().st_size
    if shutil.disk_usage(target_dir).free < size + 2 * 1024 * 1024 * 1024:
        return None
    target = target_dir / snapshot.name
    writing = target.with_name(f"{target.name}.part")
    shutil.copyfile(snapshot, writing)
    os.replace(writing, target)
    for old in snapshots(folder)[keep:]:
        old.unlink(missing_ok=True)
    return target
