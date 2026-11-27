"""Each novel's place on its site's own popularity ranking.

A discovery adds every novel a site has, the good with the bad, and getting
their chapters went in the order they were found. Sites rank their novels
(NovelFire by reads and ratings, MangaDex by followers): walked here, each
novel in the library gets its place, so downloads can start from the top.
"""

import logging
import re
import threading
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import sqlmodel as sq

from ..context import ctx
from ..dao import Novel
from ..utils.time_utils import current_timestamp

logger = logging.getLogger(__name__)

_SERIES = re.compile(r"/series/([0-9A-Z]{26})")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# Saved this many at a time while a ranking is walked.
_SAVE_EVERY = 200

_lock = threading.Lock()
_state: Dict[str, Any] = {
    "running": False,
    "site": "",
    "sites_done": 0,
    "sites": 0,
    "looked": 0,
    "ranked": 0,
    "error": "",
}


def url_key(url: str) -> str:
    """The same novel's address, however a site or a link wrote it."""
    found = _UUID.search(url.lower())
    if found:
        return found.group(0)  # MangaDex: /title/<id>, with or without its slug
    series = _SERIES.search(url)
    if series:
        return series.group(1)  # Weeb Central: /series/<id>, with or without its slug
    parts = urlsplit(url.strip().lower())
    host = parts.netloc[4:] if parts.netloc.startswith("www.") else parts.netloc
    return host + parts.path.rstrip("/")


def status() -> Dict[str, Any]:
    with _lock:
        return dict(_state)


def rankable_sites() -> Dict[str, int]:
    """The library's sites whose source can rank, with how many novels each has."""
    with ctx.db.session() as sess:
        counted = sess.exec(
            sq.select(Novel.domain, sq.func.count()).group_by(sq.col(Novel.domain))
        ).all()
    sites: Dict[str, int] = {}
    for domain, count in counted:
        try:
            crawler = ctx.sources.get_crawler(domain)
        except Exception:
            continue
        if crawler is not None and getattr(crawler, "can_rank", False):
            sites[domain] = int(count)
    return sites


def start(user_id: str, sites: Optional[List[str]] = None) -> Dict[str, Any]:
    with _lock:
        if _state["running"]:
            return dict(_state)
        _state.update(running=True, site="", sites_done=0, sites=0, looked=0, ranked=0, error="")
    threading.Thread(target=_walk, args=(user_id, sites), daemon=True, name="ranking").start()
    return status()


def _walk(user_id: str, only: Optional[List[str]]) -> None:
    try:
        sites = [site for site in rankable_sites() if not only or site in only]
        with _lock:
            _state["sites"] = len(sites)
        for site in sites:
            with _lock:
                _state["site"] = site
            try:
                _rank_site(user_id, site)
            except Exception as error:
                logger.warning(f"Could not read the ranking of {site}: {error}")
                with _lock:
                    _state["error"] = _state["error"] or f"{site}: {error}"
            with _lock:
                _state["sites_done"] += 1
    finally:
        with _lock:
            _state["running"] = False
            _state["site"] = ""


def _rank_site(user_id: str, site: str) -> None:
    with ctx.db.session() as sess:
        novels = sess.exec(sq.select(Novel.id, Novel.url).where(Novel.domain == site)).all()
    by_key = {url_key(url): novel_id for novel_id, url in novels if url}
    if not by_key:
        return
    left = set(by_key.values())
    found: Dict[str, int] = {}
    with ctx.crawler.prepare_crawler(user_id, novels[0][1]) as crawler:
        for place, url in enumerate(crawler.popular(), start=1):
            with _lock:
                _state["looked"] += 1
            novel_id = by_key.get(url_key(url))
            if novel_id and novel_id in left:
                left.discard(novel_id)
                found[novel_id] = place
                with _lock:
                    _state["ranked"] += 1
                if len(found) >= _SAVE_EVERY:
                    _save(found)
                    found = {}
            if not left:
                break  # every novel of the library on this site has its place
    _save(found)


def _save(ranks: Dict[str, int]) -> None:
    if not ranks:
        return
    now = current_timestamp()
    with ctx.db.session() as sess:
        for novel in sess.exec(sq.select(Novel).where(sq.col(Novel.id).in_(list(ranks)))).all():
            extra = dict(novel.extra or {})
            extra["rank"] = ranks[novel.id]
            extra["rank_at"] = now
            novel.extra = extra
            sess.add(novel)
        sess.commit()
