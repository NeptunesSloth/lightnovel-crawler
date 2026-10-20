"""Read novels back in from archives someone already has.

An airgapped machine cannot download anything, so the ebooks carried over on a
USB stick are the only copy of a library there. This reads those files back into
the database: an export zip full of ebooks, a single ebook, or the nested zip a
json/text artifact comes as.

How much comes back depends on the format. An EPUB this app wrote carries
everything needed to rebuild the entry — the novel id as the package
identifier, the source URL on the intro page, one XHTML per chapter, and the
volume pages the chapters follow in the spine — so the novel is readable again
rather than merely on file. A json artifact carries the same thing as data and
is the most faithful of all. Formats whose text cannot be recovered (cbz, pdf,
mobi) are kept as artifacts against the novel they name, which at least puts
them back in the library and on the download list.

Imports are matched to existing novels by source URL, so re-importing an archive
updates what is already there instead of duplicating it.
"""

from dataclasses import dataclass, field
import io
import json
import logging
from pathlib import Path
import posixpath
import re
from threading import Event
from typing import Callable, Dict, List, Optional, Tuple, Union
import xml.etree.ElementTree as ET
import zipfile

import sqlmodel as sq

from ..context import ctx
from ..core import Chapter as CrawlerChapter, Volume as CrawlerVolume
from ..dao import Artifact, Chapter, Novel, OutputFormat
from ..exceptions import AbortedException
from ..utils.file_tools import safe_filename
from ..utils.url_tools import extract_host

logger = logging.getLogger(__name__)

# Where a novel with no recoverable source URL is filed. It has to be a URL
# because that column is the unique key every novel is matched on, and it has to
# be stable so a second import of the same book updates the first rather than
# landing beside it.
IMPORTED_HOST = "imported.local"

# Guard against an archive that expands far beyond its size. Nothing this app
# exports comes close, so a member above this is not a novel we wrote.
MAX_MEMBER_BYTES = 512 * 1024 * 1024

_CONTAINER = "META-INF/container.xml"
_NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}

_RE_CHAPTER_FILE = re.compile(r"chapter_(\d+)\.x?html?$", re.I)
_RE_VOLUME_FILE = re.compile(r"volume_(\d+)\.x?html?$", re.I)

# `Title.json.zip` and `Title.txt.zip` are archives in their own right, so the
# last suffix alone does not say what they hold.
_DOUBLE_SUFFIX = {
    ".json.zip": OutputFormat.json,
    ".txt.zip": OutputFormat.text,
}
_SUFFIX_FORMAT = {f".{fmt.value}": fmt for fmt in OutputFormat}
_SUFFIX_FORMAT[".txt"] = OutputFormat.text


@dataclass
class ParsedChapter:
    serial: int
    title: str
    body: str
    volume: Optional[int] = None


@dataclass
class ParsedBook:
    """What could be recovered from one ebook."""

    title: str
    source_url: Optional[str] = None
    identifier: Optional[str] = None
    authors: Optional[str] = None
    synopsis: Optional[str] = None
    language: Optional[str] = None
    rtl: bool = False
    tags: List[str] = field(default_factory=list)
    cover: Optional[bytes] = None
    volumes: List[Tuple[int, str]] = field(default_factory=list)
    chapters: List[ParsedChapter] = field(default_factory=list)


@dataclass
class ImportStats:
    novels: int = 0
    chapters: int = 0
    updated: int = 0
    attached: int = 0
    failed: int = 0
    reasons: Dict[str, int] = field(default_factory=dict)

    def fail(self, reason: str) -> None:
        self.failed += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


ProgressFn = Callable[[str, "ImportStats"], None]


# --------------------------------------------------------------------------- #
#                              Reading the archive                             #
# --------------------------------------------------------------------------- #


def format_of(name: str) -> Optional[OutputFormat]:
    """The output format a member name claims to be, if any."""
    low = name.lower()
    for suffix, fmt in _DOUBLE_SUFFIX.items():
        if low.endswith(suffix):
            return fmt
    dot = low.rfind(".")
    if dot < 0:
        return None
    return _SUFFIX_FORMAT.get(low[dot:])


def _iter_books(source: Union[Path, bytes], name: str, signal: Event):
    """Yield every ebook in `source` as (member name, bytes, format).

    An export zip holds the ebooks flat, so one level of unwrapping is enough —
    except for json and text artifacts, which are themselves zips and are handed
    over whole for their own parser to open.

    A path is read a member at a time rather than loaded whole: an archive of a
    whole source can be hundreds of megabytes, and only one novel is needed in
    memory at once.
    """
    fmt = format_of(name)
    if fmt is not None:
        # The name already states its format, so this is the book itself.
        yield name, _read_source(source), fmt
        return

    opener: Union[Path, io.BytesIO]
    opener = source if isinstance(source, Path) else io.BytesIO(source)
    if not zipfile.is_zipfile(opener):
        return
    if isinstance(opener, io.BytesIO):
        opener.seek(0)

    with zipfile.ZipFile(opener) as zf:
        for info in zf.infolist():
            if signal.is_set():
                raise AbortedException()
            if info.is_dir() or info.file_size > MAX_MEMBER_BYTES:
                continue
            member = posixpath.basename(info.filename)
            if not member or member.startswith("."):
                continue
            member_fmt = format_of(member)
            if not member_fmt:
                continue
            try:
                yield member, zf.read(info), member_fmt
            except Exception as e:
                logger.warning(f"Could not read {info.filename}: {e}")


def _read_source(source: Union[Path, bytes]) -> bytes:
    return source.read_bytes() if isinstance(source, Path) else source


# --------------------------------------------------------------------------- #
#                                  EPUB                                        #
# --------------------------------------------------------------------------- #


def _text_of(node: Optional[ET.Element]) -> str:
    return (node.text or "").strip() if node is not None else ""


def _opf_path(zf: zipfile.ZipFile) -> Optional[str]:
    try:
        root = ET.fromstring(zf.read(_CONTAINER))
    except Exception:
        return None
    rootfile = root.find("c:rootfiles/c:rootfile", _NS)
    if rootfile is None:
        return None
    return rootfile.get("full-path")


def _strip_html(markup: str) -> str:
    from bs4 import BeautifulSoup

    return BeautifulSoup(markup, "html.parser").get_text(" ", strip=True)


def _chapter_body(markup: str) -> Tuple[str, str]:
    """The title and inner HTML of one chapter page.

    Our own pages wrap everything in `div#chapter` and open with the serial in
    an `h4` and the title in an `h1`; both are rebuilt on the way out, so they
    are dropped here rather than stored twice. Anything else is treated as a
    plain document and taken whole.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "html.parser")
    node = soup.find(id="chapter") or soup.body or soup
    title = ""

    heading = node.find(["h1", "h2"])  # type: ignore[union-attr]
    if heading:
        title = heading.get_text(" ", strip=True)
        heading.decompose()
    for serial_tag in node.find_all("h4", recursive=False):  # type: ignore[union-attr]
        if serial_tag.get_text(strip=True).startswith("#"):
            serial_tag.decompose()

    body = node.decode_contents().strip()  # type: ignore[union-attr]
    return title, body


def _source_url(markup: str) -> Optional[str]:
    """The source link the intro page carries."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "html.parser")
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        if href.startswith("http") and "lightnovel-crawler" not in href.lower():
            return href
    return None


def parse_epub(data: bytes) -> Optional[ParsedBook]:
    """Rebuild what an EPUB holds, or None if it is not readable as one."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception:
        return None

    with zf:
        opf_name = _opf_path(zf)
        if not opf_name:
            return None
        try:
            opf = ET.fromstring(zf.read(opf_name))
        except Exception:
            return None

        base = posixpath.dirname(opf_name)
        meta = opf.find("opf:metadata", _NS)
        if meta is None:
            return None

        book = ParsedBook(title=_text_of(meta.find("dc:title", _NS)))
        book.identifier = _text_of(meta.find("dc:identifier", _NS)) or None
        book.authors = _text_of(meta.find("dc:creator", _NS)) or None
        book.synopsis = _text_of(meta.find("dc:description", _NS)) or None
        book.language = _text_of(meta.find("dc:language", _NS)) or None
        book.tags = [t for t in (_text_of(s) for s in meta.findall("dc:subject", _NS)) if t]

        spine_el = opf.find("opf:spine", _NS)
        if spine_el is not None:
            book.rtl = spine_el.get("page-progression-direction") == "rtl"

        manifest: Dict[str, Tuple[str, str, str]] = {}
        for item in opf.findall("opf:manifest/opf:item", _NS):
            item_id = item.get("id") or ""
            href = item.get("href") or ""
            if not item_id or not href:
                continue
            full = posixpath.normpath(posixpath.join(base, href)) if base else href
            manifest[item_id] = (full, item.get("media-type") or "", item.get("properties") or "")

        # cover, by the property the spec gives it or the id everyone uses
        for full, media, props in manifest.values():
            if media.startswith("image/") and "cover-image" in props:
                book.cover = _safe_read(zf, full)
                break
        if book.cover is None:
            for item_id, (full, media, _) in manifest.items():
                if media.startswith("image/") and "cover" in item_id.lower():
                    book.cover = _safe_read(zf, full)
                    break

        order = [i.get("idref") or "" for i in opf.findall("opf:spine/opf:itemref", _NS)]
        _read_spine(zf, book, manifest, order)

    if not book.title and book.chapters:
        book.title = book.chapters[0].title
    return book if book.title or book.chapters else None


def _safe_read(zf: zipfile.ZipFile, name: str) -> Optional[bytes]:
    try:
        return zf.read(name)
    except Exception:
        return None


def _read_spine(
    zf: zipfile.ZipFile,
    book: ParsedBook,
    manifest: Dict[str, Tuple[str, str, str]],
    order: List[str],
) -> None:
    """Walk the reading order, collecting volumes and chapters.

    A chapter page does not record which volume it belongs to — the spine does,
    by putting each volume's page ahead of its chapters. So the last volume seen
    is the one the chapters that follow belong to.
    """
    current_volume: Optional[int] = None
    fallback_serial = 0

    for idref in order:
        entry = manifest.get(idref)
        if not entry:
            continue
        full, media, _ = entry
        if "html" not in media:
            continue
        raw = _safe_read(zf, full)
        if raw is None:
            continue
        markup = raw.decode("utf-8", errors="replace")
        name = posixpath.basename(full)

        volume_match = _RE_VOLUME_FILE.search(name)
        if volume_match:
            current_volume = int(volume_match.group(1))
            title, _ = _chapter_body(markup)
            book.volumes.append((current_volume, title or f"Volume {current_volume}"))
            continue

        if name.lower().startswith("intro"):
            book.source_url = book.source_url or _source_url(markup)
            continue
        if name.lower().startswith(("front", "cover", "nav", "toc")):
            continue

        title, body = _chapter_body(markup)
        if not body and not title:
            continue

        chapter_match = _RE_CHAPTER_FILE.search(name)
        if chapter_match:
            serial = int(chapter_match.group(1))
        else:
            # not one of ours: number the documents in reading order
            fallback_serial += 1
            serial = fallback_serial

        book.chapters.append(
            ParsedChapter(
                serial=serial,
                title=title or f"Chapter {serial}",
                body=body,
                volume=current_volume,
            )
        )


# --------------------------------------------------------------------------- #
#                              json artifact                                   #
# --------------------------------------------------------------------------- #


def parse_json_archive(data: bytes) -> Optional[ParsedBook]:
    """Rebuild from a json artifact, which stores the entry as data."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception:
        return None

    with zf:
        names = set(zf.namelist())
        if "meta.json" not in names:
            return None
        try:
            meta = json.loads(zf.read("meta.json").decode("utf-8"))
        except Exception:
            return None

        book = ParsedBook(title=str(meta.get("title") or ""))
        book.source_url = meta.get("url") or None
        book.identifier = meta.get("id") or None
        book.authors = meta.get("authors") or None
        book.synopsis = meta.get("synopsis") or None
        book.language = meta.get("language") or None
        book.rtl = bool(meta.get("rtl"))
        book.tags = [str(t) for t in (meta.get("tags") or [])]
        if "cover.jpg" in names:
            book.cover = _safe_read(zf, "cover.jpg")

        for volume in meta.get("volumes") or []:
            serial = volume.get("serial")
            if serial is None:
                continue
            book.volumes.append((int(serial), str(volume.get("title") or f"Volume {serial}")))

        for name in sorted(n for n in names if n.endswith(".json") and n != "meta.json"):
            raw = _safe_read(zf, name)
            if raw is None:
                continue
            try:
                chapter = json.loads(raw.decode("utf-8"))
            except Exception:
                continue
            serial = chapter.get("serial")
            if serial is None:
                continue
            volume_serial = None
            head = name.split("/")[0]
            if head.isdigit():
                volume_serial = int(head)
            book.chapters.append(
                ParsedChapter(
                    serial=int(serial),
                    title=str(chapter.get("title") or f"Chapter {serial}"),
                    body=str(chapter.get("content") or ""),
                    volume=volume_serial,
                )
            )

    return book if book.title or book.chapters else None


# --------------------------------------------------------------------------- #
#                               Writing it back                                #
# --------------------------------------------------------------------------- #


def _placeholder_url(book: ParsedBook, member: str) -> str:
    slug = safe_filename(book.identifier or book.title or member).strip().replace(" ", "-")
    return f"https://{IMPORTED_HOST}/{slug.lower() or 'novel'}"


def _upsert_novel(book: ParsedBook, member: str) -> Tuple[Novel, bool]:
    url = book.source_url or _placeholder_url(book, member)
    novel = ctx.novels.find_by_url(url)
    created = novel is None
    if novel is None:
        with ctx.db.session() as sess:
            novel = Novel(url=url, title=book.title or member, domain=extract_host(url))
            sess.add(novel)
            sess.commit()

    novel.title = book.title or novel.title or member
    novel.domain = extract_host(url) or IMPORTED_HOST
    if book.authors:
        novel.authors = book.authors
    if book.synopsis:
        novel.synopsis = book.synopsis
    if book.language:
        novel.language = book.language
    if book.tags:
        novel.tags = book.tags
    novel.rtl = novel.rtl or book.rtl
    if book.volumes:
        novel.volume_count = len(book.volumes)
    if book.chapters:
        novel.chapter_count = len(book.chapters)

    extra = dict(**novel.extra)
    extra["imported_from"] = member
    if book.identifier:
        extra["imported_id"] = book.identifier
    novel.extra = extra

    with ctx.db.session() as sess:
        sess.merge(novel)
        sess.commit()
    return novel, created


def _write_chapters(novel: Novel, book: ParsedBook, signal: Event) -> int:
    """Store the recovered text and mark those chapters complete."""
    written = 0
    done_serials: List[int] = []
    for parsed in book.chapters:
        if signal.is_set():
            raise AbortedException()
        if not parsed.body.strip():
            continue
        chapter_file = f"novels/{novel.id}/chapters/{parsed.serial:06}.zst"
        ctx.files.save_text(chapter_file, parsed.body)
        done_serials.append(parsed.serial)
        written += 1

    if done_serials:
        with ctx.db.session() as sess:
            sess.exec(
                sq.update(Chapter)  # type: ignore[call-overload]
                .where(sq.col(Chapter.novel_id) == novel.id)
                .where(sq.col(Chapter.serial).in_(done_serials))
                .values(is_done=True)
            )
            sess.commit()

    # keep the cached count honest, since the library lists it
    with ctx.db.session() as sess:
        total = len(sess.exec(sq.select(Chapter).where(sq.col(Chapter.novel_id) == novel.id)).all())
        if total and total != novel.chapter_count:
            novel.chapter_count = total
            sess.merge(novel)
            sess.commit()
    return written


def _attach_artifact(novel: Novel, member: str, data: bytes, fmt: OutputFormat, user_id) -> None:
    """Keep the file itself, so the library can hand it back unchanged."""
    artifact = Artifact(
        novel_id=novel.id,
        user_id=user_id,
        job_id=None,
        format=fmt,
        file_name=member,
        file_size=len(data),
    )
    ctx.files.save(artifact.output_file, data)
    with ctx.db.session() as sess:
        sess.add(artifact)
        sess.commit()


def import_book(
    member: str,
    data: bytes,
    fmt: OutputFormat,
    user_id: Optional[str],
    stats: ImportStats,
    signal: Event,
) -> None:
    book: Optional[ParsedBook] = None
    if fmt == OutputFormat.epub:
        book = parse_epub(data)
    elif fmt == OutputFormat.json:
        book = parse_json_archive(data)

    if book is None:
        # The text cannot be recovered from this format, but the file is still
        # the reader's copy of the novel — file it under the title it names.
        book = ParsedBook(title=safe_filename(member.rsplit(".", 1)[0]) or member)

    novel, created = _upsert_novel(book, member)

    if book.cover:
        try:
            ctx.files.save(novel.cover_file, book.cover)
        except Exception as e:
            logger.warning(f"Could not store the cover for {novel.title}: {e}")

    if book.tags:
        ctx.tags.insert(book.tags)

    if book.volumes:
        ctx.volumes.sync(
            novel.id,
            [CrawlerVolume(id=serial, title=title) for serial, title in book.volumes],
        )
    if book.chapters:
        ctx.chapters.sync(
            novel.id,
            [
                CrawlerChapter(
                    id=parsed.serial,
                    title=parsed.title,
                    volume=parsed.volume,
                    url=f"{novel.url}#{parsed.serial}",
                )
                for parsed in book.chapters
            ],
        )
        stats.chapters += _write_chapters(novel, book, signal)

    _attach_artifact(novel, member, data, fmt, user_id)
    stats.attached += 1
    if created:
        stats.novels += 1
    else:
        stats.updated += 1


def import_archive(
    source: Union[Path, bytes],
    name: str,
    user_id: Optional[str] = None,
    signal: Optional[Event] = None,
    on_progress: Optional[ProgressFn] = None,
) -> ImportStats:
    """Import every novel found in one uploaded file."""
    signal = signal or Event()
    stats = ImportStats()

    for member, member_data, fmt in _iter_books(source, name, signal):
        if signal.is_set():
            raise AbortedException()
        try:
            import_book(member, member_data, fmt, user_id, stats, signal)
        except AbortedException:
            raise
        except Exception as e:
            logger.warning(f"Could not import {member}: {e}")
            stats.fail(type(e).__name__)
        if on_progress:
            on_progress(member, stats)

    return stats
