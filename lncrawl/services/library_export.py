"""Build an offline ebook library, including books retained only in export ZIPs.

The same novel is usually on disk several times over: the EPUB a download made,
the copy inside each source export it was part of, and a newer one from every
later update. Those copies differ byte for byte (each build stamps its own
time), so they are told apart by the novel they hold instead — the source URL on
the intro page, else the package identifier — and only the most complete copy
goes into the library. Books that say nothing about where they came from are
kept unless they are identical.

Manga exported as CBZ is included beside the EPUBs. An NSFW filter can leave out
(or keep only) the novels whose tags mark them as adult content, and the export
can be held to novels whose every chapter is downloaded.
"""

from contextlib import ExitStack
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import posixpath
import re
from tempfile import TemporaryFile
from threading import Event, Lock, Thread
import time
from typing import IO, Any, Callable, Dict, Iterator, List, Literal, Optional, Set, Tuple
import xml.etree.ElementTree as ET
import zipfile

from .scheduler.handlers.export_source import _build_index_html

NsfwMode = Literal["include", "exclude", "only"]
# The chapter count of a fully downloaded novel, found by its id in the library
# or else by its address; None when it is not fully downloaded or not known.
FullLength = Callable[[Optional[str], Optional[str]], Optional[int]]
# What it is doing ("finding", "reading", "writing"), how far through, and for
# writing, how many bytes of how many.
Progress = Callable[..., None]


class ZipStopped(Exception):
    """The library ZIP was asked to stop."""


# Tags sources use for adult content. Matched whole, ignoring case and the
# spacing or punctuation between words ("R-18", "r18", "18+").
NSFW_TAGS: Set[str] = {
    "adult",
    "adult content",
    "mature",
    "mature content",
    "smut",
    "ecchi",
    "hentai",
    "erotica",
    "erotic",
    "eroge",
    "r18",
    "r 18",
    "18+",
    "nsfw",
    "xxx",
    "porn",
    "pornographic",
    "sexual content",
    "explicit",
    "lemon",
}

_BOOK_SUFFIXES = (".epub", ".cbz")
_RE_TAG_NOISE = re.compile(r"[\s\-_./]+")
_RE_CBZ_PAGE = re.compile(r"^(\d{5})_\d{4}\.\w+$")
_NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}


def is_nsfw(tags: List[str]) -> bool:
    for tag in tags:
        norm = _RE_TAG_NOISE.sub(" ", tag.strip().lower()).strip()
        if norm in NSFW_TAGS or norm.replace(" ", "") in NSFW_TAGS:
            return True
    return False


@dataclass
class _Book:
    """One copy of a book found on disk, and what it says about itself."""

    name: str  # file name it came under
    digest: bytes
    size: int
    suffix: str
    title: str
    key: Optional[str] = None  # which novel this is, when the book says
    novel_id: Optional[str] = None  # the library's novel it was built from, when known
    chapters: int = 0
    modified: str = ""
    tags: List[str] = field(default_factory=list)
    # how to read it again: a file on disk, or a member of an export zip
    path: Optional[Path] = None
    archive: Optional[Path] = None
    member: Optional[str] = None


def _text(node: Optional[ET.Element]) -> str:
    return (node.text or "").strip() if node is not None else ""


def _read_epub(zf: zipfile.ZipFile, book: _Book) -> None:
    from .importer import _opf_path, _source_url

    opf_name = _opf_path(zf)
    if not opf_name:
        return
    opf = ET.fromstring(zf.read(opf_name))
    meta = opf.find("opf:metadata", _NS)
    if meta is None:
        return
    book.title = _text(meta.find("dc:title", _NS)) or book.title
    book.tags = [t for t in (_text(s) for s in meta.findall("dc:subject", _NS)) if t]
    for node in meta.findall("opf:meta", _NS):
        if node.get("property") == "dcterms:modified":
            book.modified = _text(node)
    identifier = _text(meta.find("dc:identifier", _NS))

    base = posixpath.dirname(opf_name)
    hrefs = [
        posixpath.join(base, item.get("href") or "")
        for item in opf.findall("opf:manifest/opf:item", _NS)
        if "html" in (item.get("media-type") or "")
    ]
    ours = [h for h in hrefs if posixpath.basename(h).lower().startswith("chapter_")]
    book.chapters = len(ours) or len(opf.findall("opf:spine/opf:itemref", _NS))

    source = None
    intro = next((h for h in hrefs if posixpath.basename(h).lower().startswith("intro")), None)
    if intro and intro in zf.namelist():
        source = _source_url(zf.read(intro).decode("utf-8", errors="replace"))
    if source:
        book.key = f"url:{source}"
    elif identifier:
        book.key = f"id:{identifier}"


def _read_cbz(zf: zipfile.ZipFile, book: _Book) -> None:
    serials = set()
    for name in zf.namelist():
        match = _RE_CBZ_PAGE.match(posixpath.basename(name))
        if match:
            serials.add(match.group(1))
    book.chapters = len(serials)


def _fingerprint(zf: zipfile.ZipFile) -> bytes:
    """What a book holds, from its own list of files: name, checksum and size of each.

    Read from the end of the file, not the whole of it: on a library of
    hundreds of gigabytes on an external drive, reading every book just to
    tell copies apart took hours before the first one was written. Two copies
    holding the same files are the same book, whatever their zip timestamps.
    """
    digest = hashlib.sha256()
    for info in sorted(zf.infolist(), key=lambda item: item.filename):
        digest.update(f"{info.filename}\0{info.CRC}\0{info.file_size}\n".encode())
    return digest.digest()


def _describe(zf: zipfile.ZipFile, name: str, size: int, novel_tags: List[str]) -> _Book:
    stem, suffix = posixpath.splitext(name)
    book = _Book(
        name=name,
        digest=_fingerprint(zf),
        size=size,
        suffix=suffix.lower(),
        title=stem,
    )
    try:
        if book.suffix == ".epub":
            _read_epub(zf, book)
        else:
            _read_cbz(zf, book)
    except Exception:
        pass  # still a book; it just says nothing about itself
    if novel_tags and not book.tags:
        book.tags = list(novel_tags)
    return book


def _inspect_file(file: Path, novel_tags: List[str]) -> Optional[_Book]:
    """A book saved as a file, read where it is; None if it is not one."""
    try:
        with zipfile.ZipFile(file) as zf:
            return _describe(zf, file.name, file.stat().st_size, novel_tags)
    except (OSError, zipfile.BadZipFile, ValueError):
        return None


def _inspect(
    source: IO[bytes], name: str, novel_tags: List[str], spool_dir: Optional[Path] = None
) -> Optional[_Book]:
    """A book inside a zip, or None if unreadable.

    Copied out to ``spool_dir`` to be read, so even a very large one never has
    to fit in memory. Never the system's temp folder, which is on the system
    drive: once that was full, no export could be made to any drive.
    """
    with TemporaryFile(dir=spool_dir) as spool:
        size = 0
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            spool.write(chunk)
            size += len(chunk)
        spool.seek(0)
        try:
            with zipfile.ZipFile(spool) as zf:
                return _describe(zf, name, size, novel_tags)
        except (zipfile.BadZipFile, ValueError):
            return None


def _book_files(novels: Path) -> List[Path]:
    """The book files in the library: in each novel's folder and its ``artifacts``.

    Only there: a novel's folder also holds every chapter and, for manga, every
    page image, and one library had 1.67 million of those to look through.
    """
    found: List[Path] = []

    def is_book(entry: "os.DirEntry[str]") -> bool:
        return entry.name.lower().endswith(_BOOK_SUFFIXES) and entry.is_file()

    def scan(folder: str) -> List["os.DirEntry[str]"]:
        try:
            return list(os.scandir(folder))
        except OSError:
            return []

    for entry in scan(str(novels)):
        if is_book(entry):
            found.append(Path(entry.path))
        elif entry.is_dir():
            for inner in scan(entry.path):
                if is_book(inner):
                    found.append(Path(inner.path))
                elif inner.name == "artifacts" and inner.is_dir():
                    for parent, _, names in os.walk(inner.path):
                        found.extend(
                            Path(parent, name)
                            for name in names
                            if name.lower().endswith(_BOOK_SUFFIXES)
                        )
    # EPUBs first, then CBZs, each in path order: the order copies are weighed in
    return sorted(found, key=lambda file: (_BOOK_SUFFIXES.index(file.suffix.lower()), file))


def _scan(
    data_dir: Path,
    output: Path,
    tags_for: Callable[[str], List[str]],
    progress: Progress,
    stop: Optional[Event] = None,
) -> Iterator[_Book]:
    novels = data_dir / "novels"
    progress("finding", 0, 0)
    files = _book_files(novels)
    exports = data_dir / "exports"
    archives = sorted(exports.glob("*.zip")) if exports.is_dir() else []
    total = len(files) + len(archives)
    progress("reading", 0, total)
    for index, file in enumerate(files):
        if stop is not None and stop.is_set():
            raise ZipStopped()
        # novels/<novel id>/artifacts/<file>: the library knows its tags
        rel = file.relative_to(novels).parts
        tags = tags_for(rel[0]) if len(rel) > 1 else []
        book = _inspect_file(file, tags)
        progress("reading", index + 1, total)
        if book:
            book.path = file
            book.novel_id = rel[0] if len(rel) > 1 else None
            yield book

    # Bulk exports can outlive their temporary ebook files. Include those too.
    for index, file in enumerate(archives, start=len(files)):
        progress("reading", index + 1, total)
        if stop is not None and stop.is_set():
            raise ZipStopped()
        # this library's own zips hold copies of the books found here already
        library_zip = file.name == "library.zip" or file.name.startswith("library-")
        if file.resolve() == output.resolve() or file.name.startswith("backup-") or library_zip:
            continue
        try:
            archive = zipfile.ZipFile(file)
        except zipfile.BadZipFile:
            continue
        with archive:
            for member in archive.infolist():
                if stop is not None and stop.is_set():
                    raise ZipStopped()
                low = member.filename.lower()
                if member.is_dir() or not low.endswith(_BOOK_SUFFIXES):
                    continue
                name = member.filename.replace("\\", "/").rsplit("/", 1)[-1]
                with archive.open(member) as source:
                    book = _inspect(source, name, [], output.parent)
                if book:
                    book.archive = file
                    book.member = member.filename
                    yield book


def _pick(books: List[_Book]) -> List[_Book]:
    """One copy of each book: identical files once, each novel's best copy once."""
    by_digest: Dict[bytes, _Book] = {}
    for book in books:
        by_digest.setdefault(book.digest, book)

    best: Dict[str, _Book] = {}
    for book in by_digest.values():
        key = book.key or f"sha:{book.digest.hex()}"
        current = best.get(key)
        rank = (book.chapters, book.modified, book.size)
        if current is None or rank > (current.chapters, current.modified, current.size):
            best[key] = book
    return list(best.values())


def _whole(book: _Book, full_length: FullLength) -> bool:
    """Whether ``book`` holds a novel whose every chapter is downloaded."""
    url = book.key[len("url:") :] if book.key and book.key.startswith("url:") else None
    total = full_length(book.novel_id, url)
    if total is None:
        return False
    # A book built before the last chapters came in is not the whole novel. A
    # CBZ only counts chapters that have pages, so it cannot say.
    return book.suffix == ".cbz" or book.chapters >= total


def build_library_zip(
    data_dir: Path,
    output: Path,
    nsfw: NsfwMode = "include",
    tags_for: Optional[Callable[[str], List[str]]] = None,
    full_length: Optional[FullLength] = None,
    left_out: Optional[List[str]] = None,
    progress: Optional[Progress] = None,
    stop: Optional[Event] = None,
) -> int:
    """Write every book in the library to `output`, with an offline index.

    `tags_for` looks up a novel's tags by its id, for books whose own metadata
    carries none (CBZ never does). With `full_length`, only books holding a
    fully downloaded novel go in, and the titles of the others are added to
    `left_out`. Returns the number of books written. ``progress`` hears how far
    it is; ``stop`` set, it stops (`ZipStopped`), leaving `output` half written.
    """
    say: Progress = progress or (lambda *args, **kwargs: None)
    found = list(_scan(data_dir, output, tags_for or (lambda _: []), say, stop))
    chosen = _pick(found)
    if nsfw == "exclude":
        chosen = [b for b in chosen if not is_nsfw(b.tags)]
    elif nsfw == "only":
        chosen = [b for b in chosen if is_nsfw(b.tags)]
    if full_length is not None:
        verdicts = [(b, _whole(b, full_length)) for b in chosen]
        if left_out is not None:
            left_out.extend(b.title for b, whole in verdicts if not whole)
        chosen = [b for b, whole in verdicts if whole]
    status = "complete" if full_length is not None else "unknown"

    entries: List[Tuple[str, str, str]] = []
    names: Set[str] = set()
    size_total = sum(book.size for book in chosen)
    size_done = 0
    say("writing", 0, len(chosen), size_done=0, size_total=size_total)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED, allowZip64=True) as target:
        ordered = sorted(chosen, key=lambda b: (b.title.lower(), b.name.lower()))
        for done, book in enumerate(ordered, start=1):
            if stop is not None and stop.is_set():
                raise ZipStopped()
            stem = posixpath.splitext(book.name)[0]
            arcname = f"books/{stem}{book.suffix}"
            counter = 2
            while arcname.casefold() in names:
                arcname = f"books/{stem} ({counter}){book.suffix}"
                counter += 1
            try:
                with ExitStack() as stack:
                    if book.path is not None:
                        source = stack.enter_context(book.path.open("rb"))
                    else:
                        archive = stack.enter_context(zipfile.ZipFile(str(book.archive)))
                        source = stack.enter_context(archive.open(str(book.member)))
                    dest = stack.enter_context(target.open(arcname, "w", force_zip64=True))
                    while True:
                        if stop is not None and stop.is_set():
                            raise ZipStopped()
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        dest.write(chunk)
                        size_done += len(chunk)
                        say(
                            "writing",
                            done - 1,
                            len(ordered),
                            size_done=size_done,
                            size_total=size_total,
                        )
            except (OSError, KeyError, zipfile.BadZipFile):
                continue
            finally:
                say("writing", done, len(ordered), size_done=size_done, size_total=size_total)
            names.add(arcname.casefold())
            label = book.title + (" (manga)" if book.suffix == ".cbz" else "")
            entries.append((arcname, label, status))

        title = "Saved library"
        if nsfw == "exclude":
            title += " (NSFW hidden)"
        elif nsfw == "only":
            title += " (NSFW only)"
        if full_length is not None:
            title += " (fully downloaded only)"
        index = _build_index_html(title, entries)
        index = index.replace("(incomplete)", "(completeness not checked)")
        target.writestr("index.html", index)
        only_whole = (
            "Only novels with every chapter downloaded are included.\n"
            if full_length is not None
            else ""
        )
        target.writestr(
            "README.txt",
            "Extract this ZIP, then open index.html to browse your EPUBs and CBZ manga.\n"
            "Each novel is included once: the copy with the most chapters, or the newest.\n"
            f"{only_whole}"
            "This export does not download or repair chapters.\n",
        )
    return len(entries)


class LibraryZipJob:
    """One library ZIP at a time, made in the background, with how far it has got.

    Made while the request waited, a big library on an external drive took
    hours with nothing on screen but "Packing all saved books…".
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._stop = Event()
        self._status: Dict[str, Any] = {"state": "idle"}

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def start(self, work: Callable[[Progress, Event], Dict[str, Any]]) -> Dict[str, Any]:
        """Run ``work(progress, stop)`` in the background; ValueError if one is running.

        ``work`` returns what to show when it is done.
        """
        with self._lock:
            if self._status.get("state") == "running":
                raise ValueError("A library ZIP is already being made")
            self._stop = Event()
            stop = self._stop
            self._status = {
                "state": "running",
                "phase": "finding",
                "done": 0,
                "total": 0,
                "size_done": 0,
                "size_total": 0,
                "started_at": time.time(),
            }
        Thread(target=self._run, args=(work, stop), daemon=True, name="library-zip").start()
        return self.status()

    def stop(self) -> Dict[str, Any]:
        self._stop.set()
        return self.status()

    def close(self) -> None:
        self._stop.set()

    def _progress(self, phase: str, done: int, total: int, **sizes: int) -> None:
        with self._lock:
            self._status.update(phase=phase, done=done, total=total, **sizes)

    def _run(self, work: Callable[[Progress, Event], Dict[str, Any]], stop: Event) -> None:
        try:
            result = work(self._progress, stop)
            with self._lock:
                self._status.update(state="done", **result)
        except ZipStopped:
            with self._lock:
                self._status.update(state="stopped")
        except Exception as error:
            message = (
                getattr(error, "extra", None)
                or getattr(error, "detail", None)
                or str(error)
                or type(error).__name__
            )
            with self._lock:
                self._status.update(state="failed", error=str(message))
