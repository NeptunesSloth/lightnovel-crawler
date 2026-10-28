"""Build an offline ebook library, including books retained only in export ZIPs.

The same novel is usually on disk several times over: the EPUB a download made,
the copy inside each source export it was part of, and a newer one from every
later update. Those copies differ byte for byte (each build stamps its own
time), so they are told apart by the novel they hold instead — the source URL on
the intro page, else the package identifier — and only the most complete copy
goes into the library. Books that say nothing about where they came from are
kept unless they are identical.

Manga exported as CBZ is included beside the EPUBs. An NSFW filter can leave out
(or keep only) the novels whose tags mark them as adult content.
"""

from contextlib import ExitStack
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import posixpath
import re
import shutil
from tempfile import TemporaryFile
from typing import IO, Callable, Dict, Iterator, List, Literal, Optional, Set, Tuple
import xml.etree.ElementTree as ET
import zipfile

from .scheduler.handlers.export_source import _build_index_html

NsfwMode = Literal["include", "exclude", "only"]

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


def _inspect(source: IO[bytes], name: str, novel_tags: List[str]) -> Optional[_Book]:
    """Hash a book and read what it says about itself, or None if unreadable."""
    digest = hashlib.sha256()
    size = 0
    with TemporaryFile() as spool:
        # Spool to disk: even very large books never need to fit in memory.
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            spool.write(chunk)
            size += len(chunk)
        spool.seek(0)
        if not zipfile.is_zipfile(spool):
            return None
        spool.seek(0)
        stem, suffix = posixpath.splitext(name)
        book = _Book(
            name=name,
            digest=digest.digest(),
            size=size,
            suffix=suffix.lower(),
            title=stem,
        )
        try:
            with zipfile.ZipFile(spool) as zf:
                if book.suffix == ".epub":
                    _read_epub(zf, book)
                else:
                    _read_cbz(zf, book)
        except Exception:
            pass  # still a book; it just says nothing about itself
    if novel_tags and not book.tags:
        book.tags = list(novel_tags)
    return book


def _scan(
    data_dir: Path,
    output: Path,
    tags_for: Callable[[str], List[str]],
) -> Iterator[_Book]:
    novels = data_dir / "novels"
    for suffix in _BOOK_SUFFIXES:
        for file in sorted(novels.rglob(f"*{suffix}")):
            # novels/<novel id>/artifacts/<file>: the library knows its tags
            rel = file.relative_to(novels).parts
            tags = tags_for(rel[0]) if len(rel) > 1 else []
            with file.open("rb") as source:
                book = _inspect(source, file.name, tags)
            if book:
                book.path = file
                yield book

    # Bulk exports can outlive their temporary ebook files. Include those too.
    for file in sorted((data_dir / "exports").glob("*.zip")):
        if file.resolve() == output.resolve() or file.name.startswith("backup-"):
            continue
        try:
            archive = zipfile.ZipFile(file)
        except zipfile.BadZipFile:
            continue
        with archive:
            for member in archive.infolist():
                low = member.filename.lower()
                if member.is_dir() or not low.endswith(_BOOK_SUFFIXES):
                    continue
                name = member.filename.replace("\\", "/").rsplit("/", 1)[-1]
                with archive.open(member) as source:
                    book = _inspect(source, name, [])
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


def build_library_zip(
    data_dir: Path,
    output: Path,
    nsfw: NsfwMode = "include",
    tags_for: Optional[Callable[[str], List[str]]] = None,
) -> int:
    """Write every book in the library to `output`, with an offline index.

    `tags_for` looks up a novel's tags by its id, for books whose own metadata
    carries none (CBZ never does). Returns the number of books written.
    """
    found = list(_scan(data_dir, output, tags_for or (lambda _: [])))
    chosen = _pick(found)
    if nsfw == "exclude":
        chosen = [b for b in chosen if not is_nsfw(b.tags)]
    elif nsfw == "only":
        chosen = [b for b in chosen if is_nsfw(b.tags)]

    entries: List[Tuple[str, str, str]] = []
    names: Set[str] = set()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_STORED, allowZip64=True) as target:
        for book in sorted(chosen, key=lambda b: (b.title.lower(), b.name.lower())):
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
                    shutil.copyfileobj(source, dest, 1024 * 1024)
            except (OSError, KeyError, zipfile.BadZipFile):
                continue
            names.add(arcname.casefold())
            label = book.title + (" (manga)" if book.suffix == ".cbz" else "")
            entries.append((arcname, label, "unknown"))

        title = "Saved library"
        if nsfw == "exclude":
            title += " (NSFW hidden)"
        elif nsfw == "only":
            title += " (NSFW only)"
        index = _build_index_html(title, entries)
        index = index.replace("(incomplete)", "(completeness not checked)")
        target.writestr("index.html", index)
        target.writestr(
            "README.txt",
            "Extract this ZIP, then open index.html to browse your EPUBs and CBZ manga.\n"
            "Each novel is included once: the copy with the most chapters, or the newest.\n"
            "This export does not download or repair chapters.\n",
        )
    return len(entries)
