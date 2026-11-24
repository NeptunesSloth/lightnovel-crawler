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
from typing import Dict, List, Optional, Tuple
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


def restore(image_id: str, chapter_id: str) -> Optional[Path]:
    """The page's file, put back if the novel's e-book has it; None when it cannot be."""
    image_id = image_id.lower()
    if not _RE_IMAGE_ID.match(image_id):
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
    return None
