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
is the most faithful of all. Manga comes back with its pages: the images an
EPUB's chapters show, and the pages of a CBZ (one chapter per folder, or per
chapter number in the page names this app writes), are stored as chapter images
so the Reader can show them. Formats whose text cannot be recovered (pdf, mobi)
are kept as artifacts against the novel they name, which at least puts them back
in the library and on the download list.

Imports are matched to existing novels by source URL, so re-importing an archive
updates what is already there instead of duplicating it. An import only adds to
a novel already in the library: chapters missing from the ebook (an older copy,
or one exported before every chapter was downloaded) are kept, not deleted.
"""

from dataclasses import dataclass, field
import hashlib
import io
import json
import logging
from pathlib import Path
import posixpath
import re
import shutil
from tempfile import TemporaryDirectory
from threading import Event
from typing import Callable, Dict, List, Optional, Tuple, Union
import xml.etree.ElementTree as ET
import zipfile

import sqlmodel as sq

from ..context import ctx
from ..core import Chapter as CrawlerChapter, Volume as CrawlerVolume
from ..dao import Artifact, Chapter, ChapterImage, Novel, OutputFormat
from ..exceptions import AbortedException
from ..utils.file_tools import safe_filename
from ..utils.url_tools import extract_host

logger = logging.getLogger(__name__)

# Where a novel with no recoverable source URL is filed. It has to be a URL
# because that column is the unique key every novel is matched on, and it has to
# be stable so a second import of the same book updates the first rather than
# landing beside it.
IMPORTED_HOST = "imported.local"

# Guard against an archive that expands far beyond its size. A manga EPUB or CBZ
# can run to gigabytes, so this matches the largest upload accepted.
MAX_MEMBER_BYTES = 8 * 1024 * 1024 * 1024

# A member above this is copied to a temporary file and read from there rather
# than held in memory whole.
SPOOL_BYTES = 64 * 1024 * 1024

# Files an archive carries beside its books: the library export's README.txt
# would otherwise be read as a text ebook and filed as a novel called README.
_RE_NOT_A_BOOK = re.compile(r"^(readme|license|licence|changelog)\b", re.I)

_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif")

# The page names this app's CBZ uses: chapter serial, then page number.
_RE_CBZ_PAGE = re.compile(r"^(\d{5})_(\d{4})\.\w+$")
_RE_IMAGE_ID = re.compile(r"^[0-9a-f]{32}$")
_RE_DIGITS = re.compile(r"(\d+)")

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


# A book as it was handed over: in memory, or spooled to a file when large.
Payload = Union[bytes, Path]


@dataclass
class ParsedChapter:
    serial: int
    title: str
    body: str
    volume: Optional[int] = None
    images: List[str] = field(default_factory=list)


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
    # image id -> member of the book's own zip that holds it
    images: Dict[str, str] = field(default_factory=dict)
    manga: bool = False


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


def _iter_books(source: Payload, name: str, signal: Event, stats: "ImportStats"):
    """Yield every ebook in `source` as (member name, payload, format).

    An export zip holds the ebooks flat, so one level of unwrapping is enough —
    except for json and text artifacts, which are themselves zips and are handed
    over whole for their own parser to open.

    A path is read a member at a time rather than loaded whole: an archive of a
    whole source can be hundreds of megabytes, and only one novel is needed in
    memory at once. A member too big for that is spooled to a temporary file.
    """
    fmt = format_of(name)
    if fmt is not None:
        # The name already states its format, so this is the book itself.
        yield name, source, fmt
        return

    opener: Union[Path, io.BytesIO]
    opener = source if isinstance(source, Path) else io.BytesIO(source)
    if not zipfile.is_zipfile(opener):
        return
    if isinstance(opener, io.BytesIO):
        opener.seek(0)

    spool_root = ctx.files.resolve("imports")
    spool_root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(opener) as zf, TemporaryDirectory(dir=spool_root) as spool_dir:
        for index, info in enumerate(zf.infolist()):
            if signal.is_set():
                raise AbortedException()
            if info.is_dir():
                continue
            member = posixpath.basename(info.filename.replace("\\", "/"))
            if not member or member.startswith(".") or _RE_NOT_A_BOOK.match(member):
                continue
            member_fmt = format_of(member)
            if not member_fmt:
                continue
            if info.file_size > MAX_MEMBER_BYTES:
                logger.warning(f"Skipping {info.filename}: too large to be an ebook")
                stats.fail("TooLarge")
                continue
            payload: Payload
            try:
                if info.file_size > SPOOL_BYTES:
                    payload = Path(spool_dir) / f"{index}-{safe_filename(member)}"
                    with zf.open(info) as src, payload.open("wb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
                else:
                    payload = zf.read(info)
            except Exception as e:
                logger.warning(f"Could not read {info.filename}: {e}")
                stats.fail(type(e).__name__)
                continue
            yield member, payload, member_fmt
            if isinstance(payload, Path):
                payload.unlink(missing_ok=True)


def _title_of(member: str) -> str:
    """A title from a file name, as the reader wrote it (cased and spaced)."""
    stem = posixpath.splitext(member)[0]
    for suffix in _DOUBLE_SUFFIX:
        if member.lower().endswith(suffix):
            stem = member[: -len(suffix)]
    return stem.replace("_", " ").strip() or member


def _open_zip(data: Payload) -> zipfile.ZipFile:
    return zipfile.ZipFile(data if isinstance(data, Path) else io.BytesIO(data))


def _payload_size(data: Payload) -> int:
    return data.stat().st_size if isinstance(data, Path) else len(data)


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
    """The source link the intro page carries.

    Ours puts it in the footer, after the synopsis. A synopsis can hold links of
    its own (a translator's site, a Discord invite) that many novels share, and
    taking one of those as the source would fold all of them into one novel —
    so the footer is read first and the rest of the page only as a fallback.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "html.parser")
    footer = soup.find(class_="footer")
    anchors = footer.find_all("a", href=True) if footer else []  # type: ignore[union-attr]
    for anchor in [*anchors, *soup.find_all("a", href=True)]:
        href = str(anchor["href"])
        if href.startswith("http") and "lightnovel-crawler" not in href.lower():
            return href
    return None


def parse_epub(data: Payload) -> Optional[ParsedBook]:
    """Rebuild what an EPUB holds, or None if it is not readable as one."""
    try:
        zf = _open_zip(data)
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
    book.manga = _looks_like_manga(book)
    return book if book.title or book.chapters else None


def _image_id(book: ParsedBook, member: str) -> str:
    """The id a page image is stored under.

    Ours are already named by id; anything else gets one derived from the book
    and the member, so importing the same file again lands on the same id.
    """
    stem = posixpath.splitext(posixpath.basename(member))[0].lower()
    if _RE_IMAGE_ID.match(stem):
        return stem
    key = f"{book.identifier or book.title}:{member}"
    return hashlib.md5(key.encode()).hexdigest()


def _collect_images(
    book: ParsedBook,
    body: str,
    page: str,
    members: set,
) -> Tuple[str, List[str]]:
    """Point a chapter's images at the stored copies the Reader serves.

    Image sources are relative to the chapter page, so each is resolved against
    it to find the member it names. Each one found is renamed to
    `images/<id>.jpg`, the form stored chapters use for their images.
    """
    if "<img" not in body:
        return body, []
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(body, "html.parser")
    found: List[str] = []
    for img in soup.find_all("img", src=True):
        src = str(img["src"]).split("#")[0].split("?")[0]
        if src.startswith(("http:", "https:", "data:")):
            continue
        member = posixpath.normpath(posixpath.join(posixpath.dirname(page), src))
        if member not in members:
            continue
        image_id = _image_id(book, member)
        book.images[image_id] = member
        img["src"] = f"images/{image_id}.jpg"
        found.append(image_id)
    if not found:
        return body, []
    return soup.decode(), found


def _looks_like_manga(book: ParsedBook) -> bool:
    """Whether the chapters are pages of pictures rather than prose with art."""
    with_text = [c for c in book.chapters if c.body.strip()]
    if not with_text or not book.images:
        return False
    pictured = [c for c in with_text if c.images]
    if len(pictured) < 0.8 * len(with_text):
        return False
    words = sum(len(_strip_html(c.body)) for c in pictured[:20])
    return words < 300 * min(len(pictured), 20)


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
    members = set(zf.namelist())

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
        body, images = _collect_images(book, body, full, members)

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
                images=images,
            )
        )


# --------------------------------------------------------------------------- #
#                                   CBZ                                        #
# --------------------------------------------------------------------------- #


def _natural_key(name: str) -> List[Union[int, str]]:
    return [int(p) if p.isdigit() else p.lower() for p in _RE_DIGITS.split(name)]


def parse_cbz(data: Payload, member: str) -> Optional[ParsedBook]:
    """Rebuild a comic archive as chapters of page images.

    Ours names each page `<chapter>_<page>.jpg`, so the chapters come back by
    number. Anything else is read the way comic readers do: pages in name
    order, one chapter per folder (or a single chapter when there are none).
    """
    try:
        zf = _open_zip(data)
    except Exception:
        return None

    with zf:
        pages = sorted(
            (
                info.filename
                for info in zf.infolist()
                if not info.is_dir()
                and info.filename.lower().endswith(_IMAGE_SUFFIXES)
                and not posixpath.basename(info.filename).startswith(".")
            ),
            key=_natural_key,
        )
        if not pages:
            return None

        book = ParsedBook(title=_title_of(member))
        book.manga = True
        if "cover" in posixpath.basename(pages[0]).lower():
            book.cover = _safe_read(zf, pages[0])
            pages = pages[1:]

    ours = all(_RE_CBZ_PAGE.match(posixpath.basename(p)) for p in pages)
    groups: Dict[str, List[str]] = {}
    for page in pages:
        if ours:
            key = str(int(posixpath.basename(page)[:5]))
        else:
            key = posixpath.dirname(page)
        groups.setdefault(key, []).append(page)

    for index, (key, members) in enumerate(groups.items(), start=1):
        serial = int(key) if ours else index
        title = f"Chapter {serial}"
        if not ours and key:
            title = posixpath.basename(key)
        ids = [_image_id(book, m) for m in members]
        for image_id, page in zip(ids, members):
            book.images[image_id] = page
        body = "".join(f'<p><img src="images/{i}.jpg" alt="{i}"/></p>' for i in ids)
        book.chapters.append(ParsedChapter(serial=serial, title=title, body=body, images=ids))

    return book if book.chapters else None


# --------------------------------------------------------------------------- #
#                              json artifact                                   #
# --------------------------------------------------------------------------- #


def parse_json_archive(data: Payload) -> Optional[ParsedBook]:
    """Rebuild from a json artifact, which stores the entry as data."""
    try:
        zf = _open_zip(data)
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
    novel.manga = novel.manga or book.manga

    extra = dict(**novel.extra)
    extra["imported_from"] = member
    if book.identifier:
        extra["imported_id"] = book.identifier
    novel.extra = extra

    with ctx.db.session() as sess:
        sess.merge(novel)
        sess.commit()
    return novel, created


def _sync_structure(novel: Novel, book: ParsedBook, created: bool) -> Dict[int, Chapter]:
    """Add the book's volumes and chapters to the novel, keeping what it had.

    The sync helpers replace a novel's list outright, which suits a crawl that
    always sees the whole table of contents. An ebook only holds the chapters
    that were downloaded when it was made, so for a novel already in the library
    its list is merged in rather than put in place of the existing one — an
    older or partial copy must not delete chapters. Returns the novel's
    chapters by serial.
    """
    volumes: Dict[int, str] = {}
    chapters: Dict[int, CrawlerChapter] = {}
    if not created:
        by_id: Dict[str, int] = {}
        for vol in ctx.volumes.list(novel.id):
            volumes[vol.serial] = vol.title
            by_id[vol.id] = vol.serial
        for old in ctx.chapters.list(novel_id=novel.id):
            chapters[old.serial] = CrawlerChapter(
                id=old.serial,
                title=old.title,
                url=old.url,
                volume=by_id.get(old.volume_id or ""),
            )

    for serial, title in book.volumes:
        volumes[serial] = title
    for parsed in book.chapters:
        old_chapter = chapters.get(parsed.serial)
        url = f"{novel.url}#{parsed.serial}"
        volume = parsed.volume
        if old_chapter:
            # a crawled chapter's own URL is what a later update re-fetches
            url = old_chapter.url
            if volume is None:
                volume = old_chapter.volume
        chapters[parsed.serial] = CrawlerChapter(
            id=parsed.serial,
            title=parsed.title,
            url=url,
            volume=volume,
        )

    if volumes:
        ctx.volumes.sync(
            novel.id,
            [CrawlerVolume(id=serial, title=title) for serial, title in sorted(volumes.items())],
        )
    if chapters:
        ctx.chapters.sync(novel.id, [chapters[s] for s in sorted(chapters)])

    with ctx.db.session() as sess:
        rows = sess.exec(sq.select(Chapter).where(sq.col(Chapter.novel_id) == novel.id)).all()
        novel.volume_count = len(volumes)
        novel.chapter_count = len(rows)
        sess.merge(novel)
        sess.commit()
    return {row.serial: row for row in rows}


def _write_chapters(
    novel: Novel,
    book: ParsedBook,
    rows: Dict[int, Chapter],
    signal: Event,
) -> int:
    """Store the recovered text and mark those chapters complete.

    A chapter the library already has is left as it is: it is the copy that was
    crawled, and the ebook's is at best the same text again.
    """
    written = 0
    done_serials: List[int] = []
    for parsed in book.chapters:
        if signal.is_set():
            raise AbortedException()
        if not parsed.body.strip():
            continue
        row = rows.get(parsed.serial)
        if row is not None and row.is_done and ctx.files.exists(row.content_file):
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
    return written


def _write_images(
    novel: Novel,
    book: ParsedBook,
    data: Payload,
    rows: Dict[int, Chapter],
    signal: Event,
) -> None:
    """Store the page images chapters show, where the Reader looks for them.

    Image ids are unique across the library, so one already filed under some
    other chapter is left there; the chapter's text still names it, and the
    Reader serves it from wherever it is filed.
    """
    wanted: Dict[str, Chapter] = {}
    for parsed in book.chapters:
        row = rows.get(parsed.serial)
        if row is None:
            continue
        for image_id in parsed.images:
            wanted.setdefault(image_id, row)
    if not wanted:
        return

    with ctx.db.session() as sess:
        taken: Dict[str, str] = {}
        ids = list(wanted)
        for offset in range(0, len(ids), 500):
            for image in sess.exec(
                sq.select(ChapterImage).where(
                    sq.col(ChapterImage.id).in_(ids[offset : offset + 500])
                )
            ).all():
                taken[image.id] = image.chapter_id
        fresh = [
            ChapterImage(
                id=image_id,
                url=f"{novel.url}#{book.images[image_id]}",
                chapter_id=row.id,
                novel_id=novel.id,
                is_done=True,
            ).model_dump()
            for image_id, row in wanted.items()
            if image_id not in taken
        ]
        if fresh:
            sess.exec(sq.insert(ChapterImage), params=fresh)  # type: ignore[call-overload]
        sess.commit()

    with _open_zip(data) as zf:
        for image_id, row in wanted.items():
            if signal.is_set():
                raise AbortedException()
            if taken.get(image_id, row.id) != row.id:
                continue
            target = ctx.files.resolve(f"novels/{novel.id}/images/{image_id}.jpg")
            if target.is_file():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with zf.open(book.images[image_id]) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
            except Exception as e:
                target.unlink(missing_ok=True)
                logger.warning(f"Could not store image {book.images[image_id]}: {e}")


def _attach_artifact(
    novel: Novel,
    member: str,
    data: Payload,
    fmt: OutputFormat,
    user_id: Optional[str],
) -> None:
    """Keep the file itself, so the library can hand it back unchanged.

    The file is stored under its own name, so importing it again replaces the
    copy rather than listing it twice.
    """
    with ctx.db.session() as sess:
        artifact = sess.exec(
            sq.select(Artifact)
            .where(sq.col(Artifact.novel_id) == novel.id)
            .where(sq.col(Artifact.format) == fmt)
            .where(sq.col(Artifact.file_name) == member)
        ).first()
    if artifact is None:
        artifact = Artifact(
            novel_id=novel.id,
            user_id=user_id,
            job_id=None,
            format=fmt,
            file_name=member,
        )
    artifact.file_size = _payload_size(data)

    if isinstance(data, Path):
        target = ctx.files.resolve(artifact.output_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not (target.exists() and target.samefile(data)):
            shutil.copyfile(data, target)
    else:
        ctx.files.save(artifact.output_file, data)
    with ctx.db.session() as sess:
        sess.merge(artifact)
        sess.commit()


def import_book(
    member: str,
    data: Payload,
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
    elif fmt == OutputFormat.cbz:
        book = parse_cbz(data, member)

    if book is None:
        # The text cannot be recovered from this format, but the file is still
        # the reader's copy of the novel — file it under the title it names.
        book = ParsedBook(title=_title_of(member))

    novel, created = _upsert_novel(book, member)

    if book.cover:
        try:
            ctx.files.save(novel.cover_file, book.cover)
        except Exception as e:
            logger.warning(f"Could not store the cover for {novel.title}: {e}")

    if book.tags:
        ctx.tags.insert(book.tags)

    if book.volumes or book.chapters:
        rows = _sync_structure(novel, book, created)
        stats.chapters += _write_chapters(novel, book, rows, signal)
        if book.images:
            _write_images(novel, book, data, rows, signal)

    _attach_artifact(novel, member, data, fmt, user_id)
    stats.attached += 1
    if created:
        stats.novels += 1
    else:
        stats.updated += 1


def import_archive(
    source: Payload,
    name: str,
    user_id: Optional[str] = None,
    signal: Optional[Event] = None,
    on_progress: Optional[ProgressFn] = None,
) -> ImportStats:
    """Import every novel found in one uploaded file."""
    signal = signal or Event()
    stats = ImportStats()

    for member, member_data, fmt in _iter_books(source, name, signal, stats):
        if signal.is_set():
            raise AbortedException()
        try:
            import_book(member, member_data, fmt, user_id, stats, signal)
        except AbortedException:
            raise
        except Exception as e:
            logger.warning(f"Could not import {member}: {e}", exc_info=True)
            stats.fail(type(e).__name__)
        if on_progress:
            try:
                on_progress(member, stats)
            except Exception as e:
                # a missed progress report must not end an import halfway
                logger.warning(f"Could not report import progress: {e}")

    return stats
