"""Removing the novels that are not fully downloaded, all at once.

A discovery adds every novel it finds with only its details, and a download
stopped part way leaves a novel with some of its chapters; on a drive that is
filling up, those are what people want gone. Novels an unfinished download is
still working on are kept: removed under it, the download would add them again
or fail on files that went away.
"""

import logging
import threading
from typing import Any, Dict, List, Set, Tuple
from urllib.parse import urlsplit

import sqlmodel as sq

from ..context import ctx
from ..dao import Chapter, Job, Novel
from ..enums import JobStatus, JobType

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_state: Dict[str, Any] = {
    "running": False,
    "kind": "all",
    "done": 0,
    "total": 0,
    "failed": 0,
    "error": "",
    "current": "",
}


def _domain(url: str) -> str:
    host = urlsplit(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _kept_by_downloads() -> Tuple[Set[str], Set[str], Set[str]]:
    """(novel ids, novel addresses, site domains) unfinished downloads still use."""
    ids: Set[str] = set()
    urls: Set[str] = set()
    domains: Set[str] = set()
    with ctx.db.session() as sess:
        jobs = sess.exec(
            sq.select(Job.type, Job.extra).where(
                sq.col(Job.is_done).is_(False),
                sq.col(Job.status).in_([JobStatus.PENDING, JobStatus.RUNNING, JobStatus.PAUSED]),
            )
        ).all()
    for kind, extra in jobs:
        extra = extra or {}
        url = extra.get("url")
        if isinstance(url, str) and url:
            if kind in (JobType.EXPORT_SOURCE, JobType.DISCOVER_SOURCE, JobType.LIST_SOURCE):
                domains.add(_domain(url))  # a whole site: any of its novels
            else:
                urls.add(url)
        for key in ("urls", "novel_urls"):
            value = extra.get(key)
            if isinstance(value, list):
                urls.update(str(item) for item in value)
        novel_id = extra.get("novel_id")
        if isinstance(novel_id, str):
            ids.add(novel_id)
        novel_ids = extra.get("novel_ids")
        if isinstance(novel_ids, list):
            ids.update(str(item) for item in novel_ids)
    return ids, urls, domains


# Chapters listed this long (ms) after the newest downloaded one was listed were
# added by the site later, not left behind by a download that stopped: a novel's
# chapters are all listed together when it is first looked at.
NEW_AFTER = 60 * 60 * 1000

# Which unfinished novels to remove: with only their details, partly downloaded, or both.
KINDS = ("details", "partial", "all")


def _wanted(item: Dict[str, Any], kind: str) -> bool:
    if kind == "details":
        return not item["done"]
    if kind == "partial":
        return bool(item["done"])
    return True


def find(kind: str = "all") -> Dict[str, Any]:
    """The novels not fully downloaded: those to remove, and those kept for now.

    The counts of each kind are always given; ``novels`` holds only ``kind``.
    """
    with ctx.db.session() as sess:
        counted = sess.exec(
            sq.select(
                Chapter.novel_id,
                sq.func.count(),
                sq.func.sum(sq.case((sq.col(Chapter.is_done).is_(True), 1), else_=0)),
            ).group_by(sq.col(Chapter.novel_id))
        ).all()
        listed = sess.exec(
            sq.select(
                Chapter.novel_id,
                Chapter.is_done,
                sq.func.min(Chapter.created_at),
                sq.func.max(Chapter.created_at),
            ).group_by(sq.col(Chapter.novel_id), sq.col(Chapter.is_done))
        ).all()
        novels = sess.exec(sq.select(Novel.id, Novel.title, Novel.url)).all()
    chapters: Dict[str, Tuple[int, int]] = {
        novel_id: (int(total or 0), int(done or 0)) for novel_id, total, done in counted
    }
    newest_done: Dict[str, int] = {}
    oldest_missing: Dict[str, int] = {}
    for novel_id, is_done, first, last in listed:
        if is_done:
            newest_done[novel_id] = int(last or 0)
        else:
            oldest_missing[novel_id] = int(first or 0)
    kept_ids, kept_urls, kept_domains = _kept_by_downloads()
    remove: List[Dict[str, Any]] = []
    kept = behind = 0
    for novel_id, title, url in novels:
        total, done = chapters.get(novel_id, (0, 0))
        if total and done >= total:
            continue  # every chapter is here
        if done and oldest_missing.get(novel_id, 0) > newest_done.get(novel_id, 0) + NEW_AFTER:
            # Everything it had was downloaded; the site has added chapters
            # since. That is a novel to update, not a download that stopped.
            behind += 1
            continue
        if novel_id in kept_ids or url in kept_urls or _domain(url or "") in kept_domains:
            kept += 1
            continue
        remove.append({"id": novel_id, "title": title, "done": done, "total": total})
    return {
        "novels": [item for item in remove if _wanted(item, kind)],
        "details_only": sum(1 for item in remove if not item["done"]),
        "partial": sum(1 for item in remove if item["done"]),
        "chapters": sum(item["done"] for item in remove),
        "kept": kept,
        "behind": behind,
    }


def status() -> Dict[str, Any]:
    with _lock:
        return dict(_state)


def start(kind: str = "all") -> Dict[str, Any]:
    """Remove them in the background (a novel's folder can take a while on a drive)."""
    if kind not in KINDS:
        raise ValueError(f"Remove {', '.join(KINDS)} unfinished novels, not {kind}")
    with _lock:
        if _state["running"]:
            return dict(_state)
        found = find(kind)["novels"]
        _state.update(
            running=True, kind=kind, done=0, total=len(found), failed=0, error="", current=""
        )
    todo = [(item["id"], item["title"]) for item in found]
    threading.Thread(target=_remove, args=(todo,), daemon=True).start()
    return status()


def _remove(todo: List[Tuple[str, str]]) -> None:
    try:
        for novel_id, title in todo:
            with _lock:
                _state["current"] = title or ""
            try:
                ctx.novels.delete(novel_id)
            except Exception as error:
                logger.warning(f"Could not remove novel {novel_id}: {error}")
                with _lock:
                    _state["failed"] += 1
                    _state["error"] = _state["error"] or str(error)
            with _lock:
                _state["done"] += 1
    finally:
        with _lock:
            _state["running"] = False
            _state["current"] = ""
