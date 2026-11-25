"""Cleaning up the extras a library collects: duplicate novels, old e-books, leftovers.

Three kinds, each found first and shown, then removed in the background:

- ``duplicates``: the same story saved more than once (two sites, an import
  next to a download). Each set is merged into its most complete copy, as the
  Reader's Merge button does; a copy holding chapters the kept one lacks is
  left alone. Only copies that look like the same story are grouped: the same
  title, both manga or both not, and the same author when both name one.
- ``ebooks``: every rebuild, repair and phone download records another e-book
  for the same novel. The newest of each novel's format and language is kept;
  older records go, with their files when no kept record uses the same file,
  and e-book files no record names.
- ``leftovers``: files nothing uses: half-made books in ``tmp`` (older than a
  few hours, so not one being made now), folders of novels no longer in the
  library, here and on the book drive, and copies of the database set aside by
  a split or a repair more than two weeks ago.
"""

import logging
import os
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import sqlmodel as sq

from ..context import ctx
from ..dao import Artifact, Chapter, Novel
from ..utils.file_tools import dir_size

logger = logging.getLogger(__name__)

KINDS = ("duplicates", "ebooks", "leftovers")
# A working file in tmp younger than this may belong to a book being made now.
TMP_AGE = 6 * 3600
# Database copies set aside by a split or a repair are kept this long first.
ASIDE_AGE = 14 * 24 * 3600
_ASIDE = re.compile(r"^sqlite\.db\.(before-split|unreadable)-")

_lock = threading.Lock()
_state: Dict[str, Any] = {
    "running": False,
    "kinds": [],
    "done": 0,
    "total": 0,
    "failed": 0,
    "freed": 0,
    "merged": 0,
    "kept": 0,
    "error": "",
    "current": "",
}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _authors(text: Optional[str]) -> str:
    return _norm(text or "")


# --------------------------------------------------------------------------- #
#                                 duplicates                                   #
# --------------------------------------------------------------------------- #


def _duplicate_sets() -> List[List[Dict[str, Any]]]:
    """Copies of the same story: sets of two or more novels."""
    with ctx.db.session() as sess:
        novels = [
            (n.id, n.title, n.domain, n.authors, n.manga) for n in sess.exec(sq.select(Novel)).all()
        ]
        counted = sess.exec(
            sq.select(
                Chapter.novel_id,
                sq.func.count(),
                sq.func.sum(sq.case((sq.col(Chapter.is_done).is_(True), 1), else_=0)),
            ).group_by(sq.col(Chapter.novel_id))
        ).all()
    chapters = {novel_id: (int(total or 0), int(done or 0)) for novel_id, total, done in counted}
    by_title: Dict[Tuple[str, bool], List[Dict[str, Any]]] = {}
    for novel_id, title, domain, authors, manga in novels:
        key = _norm(title)
        if not key:
            continue
        if not re.search(r"[a-z]", key):
            key = (title or "").strip()  # no Latin letters to compare: only the same title
        total, done = chapters.get(novel_id, (0, 0))
        by_title.setdefault((key, bool(manga)), []).append(
            {
                "id": novel_id,
                "title": title,
                "domain": domain,
                "authors": authors or "",
                "chapters": total,
                "downloaded": done,
            }
        )
    sets: List[List[Dict[str, Any]]] = []
    for group in by_title.values():
        if len(group) < 2:
            continue
        # Copies naming different authors are different stories with one title.
        named = {_authors(copy["authors"]) for copy in group if _authors(copy["authors"])}
        if len(named) > 1:
            # Copies naming no author could be any of these stories: left out.
            # Only copies naming the same author are one story.
            for author in sorted(named):
                same = [c for c in group if _authors(c["authors"]) == author]
                if len(same) >= 2:
                    sets.append(sorted(same, key=lambda c: -c["downloaded"]))
            continue
        sets.append(sorted(group, key=lambda c: -c["downloaded"]))
    unique = sets
    unique.sort(key=lambda copies: (copies[0]["title"] or "").lower())
    return unique


# --------------------------------------------------------------------------- #
#                                   e-books                                    #
# --------------------------------------------------------------------------- #


def _old_ebooks() -> Tuple[List[str], List[Path]]:
    """(records to remove, files to remove) of e-books a newer one replaced."""
    with ctx.db.session() as sess:
        rows = [
            (a.id, a.novel_id, a.format, a.language, a.file_name)
            for a in sess.exec(
                sq.select(Artifact).order_by(sq.col(Artifact.created_at).desc())
            ).all()
        ]
        novel_ids = set(sess.exec(sq.select(Novel.id)).all())
    books = getattr(ctx, "books", None)
    newest: Set[Tuple[str, str, str]] = set()
    kept_files: Dict[str, Set[str]] = {}
    old_rows: List[str] = []
    old_files: Dict[str, Set[str]] = {}
    for artifact_id, novel_id, fmt, language, file_name in rows:
        if books is not None and books.unavailable(novel_id):
            continue  # its books are on a drive that is not plugged in: not looked at
        key = (novel_id, str(fmt), language or "")
        if key not in newest:
            newest.add(key)
            kept_files.setdefault(novel_id, set()).add(file_name)
            continue
        old_rows.append(artifact_id)
        old_files.setdefault(novel_id, set()).add(file_name)
    files: List[Path] = []
    for novel_id in novel_ids:
        if books is not None and books.unavailable(novel_id):
            continue
        folder = ctx.files.resolve(f"novels/{novel_id}/artifacts")
        try:
            present = [entry.name for entry in os.scandir(folder) if entry.is_file()]
        except OSError:
            continue
        kept = kept_files.get(novel_id, set())
        for name in present:
            file = folder / name
            # replaced by a newer one, or named by no record at all; a book made
            # moments ago is in place a little before its record is
            if name not in kept and not _recent(file, time.time()):
                files.append(file)
    return old_rows, files


# --------------------------------------------------------------------------- #
#                                  leftovers                                   #
# --------------------------------------------------------------------------- #


def _leftovers() -> List[Tuple[Path, str]]:
    """(file or folder, why it is not needed)."""
    found: List[Tuple[Path, str]] = []
    now = time.time()
    tmp = ctx.config.app.app_dir / "tmp"
    try:
        for entry in os.scandir(tmp):
            if now - entry.stat(follow_symlinks=False).st_mtime > TMP_AGE:
                found.append((Path(entry.path), "a working file from an earlier run"))
    except OSError:
        pass

    with ctx.db.session() as sess:
        novel_ids = set(sess.exec(sq.select(Novel.id)).all())
    roots = [ctx.files.resolve("novels")]
    books = getattr(ctx, "books", None)
    drive = books.folder() if books is not None and books.plugged() else None
    if drive is not None:
        roots.append(drive / "novels")
    for root in roots:
        try:
            entries = [entry for entry in os.scandir(root) if entry.is_dir()]
        except OSError:
            continue
        for entry in entries:
            if entry.name in novel_ids or _recent(entry, now):
                continue  # a novel, or one being added right now
            found.append((Path(entry.path), "the folder of a novel no longer in the library"))

    places = {ctx.config.app.app_dir, ctx.config.app.base_dir}
    if drive is not None:
        places.add(drive)
    for place in places:
        try:
            entries = list(os.scandir(place))
        except OSError:
            continue
        for entry in entries:
            if not entry.is_file() or not _ASIDE.match(entry.name):
                continue
            if now - entry.stat().st_mtime > ASIDE_AGE:
                found.append((Path(entry.path), "an old copy of the database, set aside"))
    return found


def _recent(path: Any, now: float) -> bool:
    try:
        return now - os.stat(path).st_mtime < TMP_AGE
    except OSError:
        return True  # cannot tell: left alone


def _size(path: Path) -> int:
    try:
        return dir_size(path) if path.is_dir() else path.stat().st_size
    except OSError:
        return 0


# --------------------------------------------------------------------------- #
#                                 find & clean                                 #
# --------------------------------------------------------------------------- #


def find() -> Dict[str, Any]:
    """What each kind would clean up, with sizes, for the page to show first."""
    sets = _duplicate_sets()
    rows, ebook_files = _old_ebooks()
    leftovers = _leftovers()
    return {
        "duplicates": {
            "sets": len(sets),
            "copies": sum(len(copies) - 1 for copies in sets),
            "list": sets[:200],
        },
        "ebooks": {
            "records": len(rows),
            "files": len(ebook_files),
            "size": sum(_size(file) for file in ebook_files),
        },
        "leftovers": {
            "count": len(leftovers),
            "size": sum(_size(path) for path, _ in leftovers),
            "list": [
                {"path": str(path), "why": why, "size": _size(path)}
                for path, why in leftovers[:200]
            ],
        },
        "cleaning": status(),
    }


def status() -> Dict[str, Any]:
    with _lock:
        return dict(_state)


def start(kinds: List[str]) -> Dict[str, Any]:
    """Clean up these kinds in the background."""
    wanted = [kind for kind in KINDS if kind in kinds]
    unknown = [kind for kind in kinds if kind not in KINDS]
    if unknown or not wanted:
        raise ValueError(f"Choose from {', '.join(KINDS)}")
    with _lock:
        if _state["running"]:
            return dict(_state)
        _state.update(
            running=True,
            kinds=wanted,
            done=0,
            total=0,
            failed=0,
            freed=0,
            merged=0,
            kept=0,
            error="",
            current="Looking for extras…",
        )
    threading.Thread(target=_clean, args=(wanted,), daemon=True, name="extras").start()
    return status()


def _remove(path: Path) -> int:
    size = _size(path)
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return size


def _clean(kinds: List[str]) -> None:
    try:
        steps: List[Tuple[str, Callable[[], None]]] = []
        if "duplicates" in kinds:
            for copies in _duplicate_sets():
                ids = [copy["id"] for copy in copies]
                steps.append((copies[0]["title"] or "", _merger(ids)))
        if "ebooks" in kinds:
            rows, files = _old_ebooks()
            if rows:
                steps.append(("Old e-book records", _forget_rows(rows)))
            for file in files:
                steps.append((file.name, _deleter(file)))
        if "leftovers" in kinds:
            for path, _ in _leftovers():
                steps.append((path.name, _deleter(path)))
        with _lock:
            _state["total"] = len(steps)
        for title, step in steps:
            with _lock:
                _state["current"] = title
            try:
                step()
            except Exception as error:
                logger.warning(f"Could not clean up {title}: {error}")
                with _lock:
                    _state["failed"] += 1
                    _state["error"] = _state["error"] or f"{title}: {error}"
            with _lock:
                _state["done"] += 1
    except Exception as error:
        logger.warning(f"Cleaning up extras stopped: {error}")
        with _lock:
            _state["error"] = str(error)
    finally:
        with _lock:
            _state["running"] = False
            _state["current"] = ""


def _merger(ids: List[str]) -> Callable[[], None]:
    def merge() -> None:
        result = ctx.novels.merge_duplicates(ids[0], among=ids)
        with _lock:
            _state["merged"] += int(result.get("merged") or 0)
            _state["kept"] += int(result.get("kept") or 0)

    return merge


def _forget_rows(rows: List[str]) -> Callable[[], None]:
    def forget() -> None:
        with ctx.db.session() as sess:
            for offset in range(0, len(rows), 500):
                sess.exec(
                    sq.delete(Artifact).where(sq.col(Artifact.id).in_(rows[offset : offset + 500]))
                )
            sess.commit()

    return forget


def _deleter(path: Path) -> Callable[[], None]:
    def delete() -> None:
        freed = _remove(path)
        with _lock:
            _state["freed"] += freed

    return delete
