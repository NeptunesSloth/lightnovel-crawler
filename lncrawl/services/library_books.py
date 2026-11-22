"""E-books for the novels that have none, before the library ZIP is made.

The library ZIP holds the e-books the library has: a site download that only
adds to the library makes none, so a library of 571 full novels gave a ZIP of
189 books. Each downloaded novel without an e-book, or whose e-book is older
than its newest chapter, gets one made from the chapters already saved.
"""

import logging
from threading import Event
from typing import Any, Callable, Dict, List, Optional, Tuple

import sqlmodel as sq

from ..context import ctx
from ..dao import Artifact, Chapter, Novel
from ..enums import OutputFormat

logger = logging.getLogger(__name__)

_BOOKS = (OutputFormat.epub, OutputFormat.cbz)


def novels_needing_books(complete_only: bool, only: Optional[str] = None) -> List[Dict[str, Any]]:
    """Downloaded novels whose e-book is missing or older than their chapters.

    ``only``: just this novel, if it needs one.
    """
    with ctx.db.session() as sess:
        chapters_query = sq.select(
            Chapter.novel_id,
            sq.func.count(),
            sq.func.sum(sq.case((sq.col(Chapter.is_done).is_(True), 1), else_=0)),
            sq.func.max(Chapter.updated_at),
        )
        made_query = (
            sq.select(Artifact.novel_id, Artifact.format, Artifact.file_name, Artifact.created_at)
            .where(sq.col(Artifact.format).in_(_BOOKS))
            .order_by(sq.col(Artifact.created_at))
        )
        novels_query = sq.select(Novel.id, Novel.title, Novel.manga)
        if only is not None:
            chapters_query = chapters_query.where(Chapter.novel_id == only)
            made_query = made_query.where(Artifact.novel_id == only)
            novels_query = novels_query.where(Novel.id == only)
        counted = sess.exec(chapters_query.group_by(sq.col(Chapter.novel_id))).all()
        made = sess.exec(made_query).all()
        novels = sess.exec(novels_query).all()
    chapters: Dict[str, Tuple[int, int, int]] = {
        novel_id: (int(total or 0), int(done or 0), int(last or 0))
        for novel_id, total, done, last in counted
    }
    newest: Dict[str, Tuple[int, str]] = {}  # novel -> (when, its file), the newest one
    for novel_id, _, file_name, created in made:
        newest[novel_id] = (int(created or 0), f"novels/{novel_id}/artifacts/{file_name}")
    books = getattr(ctx, "books", None)
    todo: List[Dict[str, Any]] = []
    for novel_id, title, manga in novels:
        total, done, last = chapters.get(novel_id, (0, 0, 0))
        if not done or (complete_only and done < total):
            continue
        if books is not None and books.unavailable(novel_id):
            continue  # its chapters are on a drive that is not plugged in
        book = newest.get(novel_id)
        if book is not None and book[0] >= last and ctx.files.exists(book[1]):
            continue  # an e-book with every chapter it has
        todo.append({"id": novel_id, "title": title, "manga": bool(manga)})
    return todo


def make_missing_books(
    complete_only: bool,
    progress: Optional[Callable[..., None]] = None,
    stop: Optional[Event] = None,
) -> Tuple[int, int]:
    """Make the e-books the library ZIP would otherwise go without; (made, failed)."""
    say = progress or (lambda *args, **kwargs: None)
    todo = novels_needing_books(complete_only)
    made = failed = 0
    for index, novel in enumerate(todo):
        if stop is not None and stop.is_set():
            break
        say("making", index, len(todo), title=novel["title"])
        try:
            ctx.binder.make_artifact(
                novel["id"],
                novel["title"],
                format=OutputFormat.cbz if novel["manga"] else OutputFormat.epub,
                signal=stop or Event(),
            )
            made += 1
        except Exception as error:
            failed += 1
            logger.warning(f"Could not make an e-book of {novel['title']}: {error}")
    if todo:
        say("making", len(todo), len(todo), title="")
    return made, failed


def book_for(novel_id: str) -> Artifact:
    """This novel's e-book with every chapter it has, made now if need be.

    For taking a novel to a phone: the Reader there cannot keep pages for
    offline reading over the plain-http address it is opened at, so the
    e-book is what goes, to open in any e-book app.
    """
    todo = novels_needing_books(False, only=novel_id)
    if todo:
        novel = todo[0]
        return ctx.binder.make_artifact(
            novel["id"],
            novel["title"],
            format=OutputFormat.cbz if novel["manga"] else OutputFormat.epub,
        )
    with ctx.db.session() as sess:
        newest = sess.exec(
            sq.select(Artifact)
            .where(Artifact.novel_id == novel_id, sq.col(Artifact.format).in_(_BOOKS))
            .order_by(sq.col(Artifact.created_at).desc())
            .limit(1)
        ).first()
    if newest is None:
        raise ValueError("No chapters of this novel are downloaded yet")
    return newest
