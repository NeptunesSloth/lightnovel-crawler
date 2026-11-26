"""Getting back a page image the library has lost, from the novel's own e-book.

A manga's pages are files beside rows in the database, and either can go
missing while the chapter still names the page: rows left behind by a deleted
novel kept an import from filing its pages, and deleting one of two copies of
a manga took the pages the other was showing. The Reader then showed
"(image unavailable)" for every page, although the novel's e-book in the
library still held them all. When a page is asked for and is not there, it is
taken out of that e-book, stored where it belongs, and served.
"""

import logging
from pathlib import Path
import posixpath
import re
import shutil
import threading
from threading import Event
from time import monotonic
from typing import Dict, List, Optional, Set, Tuple
import zipfile

import sqlmodel as sq

from ..context import ctx
from ..dao import Artifact, ChapterImage
from ..enums import OutputFormat
from ..utils.file_tools import atomic_write

logger = logging.getLogger(__name__)

_RE_IMAGE_ID = re.compile(r"^[0-9a-f]{32}$")
_RE_NAMED = re.compile(r"images/([0-9a-fA-F]{32})[.]jpg")
_lock = threading.Lock()
# e-book file -> (its size and time, {chapter serial: its pages in order}, {image id: member})
_books: Dict[str, Tuple[Tuple[int, float], Dict[int, List[str]], Dict[str, str]]] = {}
_BOOKS_KEPT = 4
_hint: Dict[str, str] = {}  # (kept for callers that clear it)
# Chapters none of whose pages could be had, and when: the rest of their pages
# are not looked for again for a while (each look was the same, slow, miss).
_given_up: Dict[str, float] = {}
GIVE_UP_FOR = 600.0
_said: Set[str] = set()  # chapters already said to be past saving, so the log says it once
# Seconds a page may take to download again while the Reader waits for it.
DOWNLOAD_WAIT = 40.0


def _layout(file: Path, name: str) -> Tuple[Dict[int, List[str]], Dict[str, str]]:
    """What pages an e-book holds: by chapter and place, and by the id in their name."""
    stat = file.stat()
    stamp = (stat.st_size, stat.st_mtime)
    key = str(file)
    known = _books.get(key)
    if known is not None and known[0] == stamp:
        return known[1], known[2]
    by_chapter: Dict[int, List[str]] = {}
    by_id: Dict[str, str] = {}
    with zipfile.ZipFile(file) as zf:
        for member in zf.namelist():
            stem = posixpath.splitext(posixpath.basename(member))[0].lower()
            if _RE_IMAGE_ID.match(stem):
                by_id[stem] = member
    if name.lower().endswith(".cbz"):
        from .importer import parse_cbz

        book = parse_cbz(file, name)
        if book is not None:
            for chapter in book.chapters:
                by_chapter[chapter.serial] = [book.images[i] for i in chapter.images]
            for image_id, member in book.images.items():
                by_id.setdefault(image_id, member)
    while len(_books) >= _BOOKS_KEPT:
        _books.pop(next(iter(_books)))
    _books[key] = (stamp, by_chapter, by_id)
    return by_chapter, by_id


def _file(image_id: str, novel_id: str, chapter_id: str) -> None:
    """Have the database say this page is this chapter's, stored with its novel."""
    with ctx.db.session() as sess:
        image = sess.get(ChapterImage, image_id)
        if image is None:
            sess.add(
                ChapterImage(
                    id=image_id,
                    url=f"restored:{image_id}",
                    chapter_id=chapter_id,
                    novel_id=novel_id,
                    is_done=True,
                )
            )
        else:
            image.novel_id = novel_id
            image.chapter_id = chapter_id
            image.is_done = True
            sess.add(image)
        sess.commit()


def _found_elsewhere(image_id: str, novel_id: str) -> Optional[Path]:
    """The page's file, filed under another novel's folder (here or on the book drive).

    Where it is filed is asked of the database. Looking in every novel's folder
    for each page took minutes a chapter on an external drive with two
    thousand novels, while the Reader showed empty boxes.
    """
    with ctx.db.session() as sess:
        image = sess.get(ChapterImage, image_id)
        owner = image.novel_id if image is not None else None
    if not owner or owner == novel_id:
        return None
    books = getattr(ctx, "books", None)
    folders = books.folders(owner) if books is not None else [ctx.files.resolve(f"novels/{owner}")]
    for folder in folders:
        candidate = Path(folder) / "images" / f"{image_id}.jpg"
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
        except OSError:
            continue
    return None


def _download(image_id: str, novel_id: str, chapter_id: str, user_id: str) -> Optional[Path]:
    """Fetch the page again from the site it came from, when its address is known."""
    with ctx.db.session() as sess:
        image = sess.get(ChapterImage, image_id)
        url = image.url if image is not None else ""
    if not url.startswith(("http://", "https://")):
        return None
    _file(image_id, novel_id, chapter_id)  # its novel may be gone: this one takes it
    stop = Event()
    timer = threading.Timer(DOWNLOAD_WAIT, stop.set)
    timer.start()
    try:
        image = ctx.crawler.fetch_image(user_id, image_id, stop, refresh=True)
    finally:
        timer.cancel()
    file = ctx.files.resolve(image.image_file)
    return file if file.is_file() and file.stat().st_size > 0 else None


def _say_once(chapter_id: str, message: str) -> None:
    if chapter_id not in _said:
        _said.add(chapter_id)
        logger.warning(message)


def restore(image_id: str, chapter_id: str, user_id: Optional[str] = None) -> Optional[Path]:
    """The page's file, put back from wherever it can be found; None when it cannot be.

    In order: a file already in the library (under this novel or another),
    the novel's own e-book, then the site it was downloaded from.
    """
    image_id = image_id.lower()
    if not _RE_IMAGE_ID.match(image_id):
        return None
    if monotonic() - _given_up.get(chapter_id, -GIVE_UP_FOR) < GIVE_UP_FOR:
        return None
    chapter = ctx.chapters.get(chapter_id)
    novel_id = chapter.novel_id
    if not ctx.files.exists(chapter.content_file):
        return None
    named = [m.lower() for m in _RE_NAMED.findall(ctx.files.load_text(chapter.content_file))]
    if image_id not in named:
        return None  # not a page of this chapter: nothing is filed on a guess
    place = named.index(image_id)
    relative = f"novels/{novel_id}/images/{image_id}.jpg"
    target = ctx.files.resolve(relative)

    with _lock:
        if target.is_file() and target.stat().st_size > 0:
            _file(image_id, novel_id, chapter_id)  # the file was there; only its row was not
            return target
        elsewhere = _found_elsewhere(image_id, novel_id)
        if elsewhere is not None:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(elsewhere, "rb") as src, atomic_write(target) as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            _file(image_id, novel_id, chapter_id)
            return target
        with ctx.db.session() as sess:
            made = sess.exec(
                sq.select(Artifact)
                .where(
                    Artifact.novel_id == novel_id,
                    sq.col(Artifact.format).in_([OutputFormat.cbz, OutputFormat.epub]),
                )
                .order_by(sq.col(Artifact.created_at).desc())
            ).all()
        for artifact in made:
            file = ctx.files.resolve(artifact.output_file)
            try:
                if not file.is_file():
                    continue
                by_chapter, by_id = _layout(file, artifact.file_name)
                member = by_id.get(image_id)
                if member is None:
                    pages = by_chapter.get(chapter.serial) or []
                    # the same chapter, the same number of pages: the same place is the same page
                    if len(pages) == len(named):
                        member = pages[place]
                if member is None:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(file) as zf:
                    with zf.open(member) as src, atomic_write(target) as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
                _file(image_id, novel_id, chapter_id)
                logger.info(f"Put back page {image_id} of novel {novel_id} from {file.name}")
                return target
            except Exception as error:
                logger.warning(f"Could not take page {image_id} out of {file}: {error}")

    if user_id:
        try:
            found = _download(image_id, novel_id, chapter_id, user_id)
            if found is not None:
                return found
        except Exception as error:
            # the site is not giving it: the chapter's other pages would wait
            # out the same failure one by one
            _given_up[chapter_id] = monotonic()
            _say_once(chapter_id, f"Could not download page {image_id} again: {error}")
            return None
    _given_up[chapter_id] = monotonic()
    held = "its e-book in the library does not hold them" if made else "it has no e-book"
    _say_once(
        chapter_id,
        f"Pages of chapter {chapter.serial} of novel {novel_id} cannot be found: not in the "
        f"library, {held}, and the site they came from is not known. "
        f"Import or download this novel again to get them back.",
    )
    return None
