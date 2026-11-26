"""Fetching again the covers the app had to draw itself.

When a novel's cover cannot be downloaded (the site refused, the address was
wrong for a while) the app draws a stand-in of coloured shapes. Each is tried
again here, from the site the novel came from, and replaced when the real one
comes down; one that still fails keeps its stand-in.
"""

import logging
import threading
from typing import Any, Dict, List, Tuple

import sqlmodel as sq

from ..context import ctx
from ..core.crawler import is_stand_in_cover
from ..dao import Novel

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_state: Dict[str, Any] = {
    "running": False,
    "done": 0,
    "total": 0,
    "fixed": 0,
    "failed": 0,
    "current": "",
}


def _stand_ins() -> Tuple[List[Tuple[str, str, str, str]], int]:
    """(novels whose cover is a stand-in and whose cover address is known, how many have none).

    Each novel as (id, title, its address, its cover's address).
    """
    with ctx.db.session() as sess:
        novels = [(n.id, n.title, n.url, n.cover_url or "") for n in sess.exec(sq.select(Novel))]
    books = getattr(ctx, "books", None)
    found: List[Tuple[str, str, str, str]] = []
    unknown = 0
    for novel_id, title, url, cover_url in novels:
        if books is not None and books.unavailable(novel_id):
            continue
        if not is_stand_in_cover(ctx.files.resolve(f"novels/{novel_id}/cover.jpg")):
            continue
        if cover_url.startswith(("http://", "https://")):
            found.append((novel_id, title, url, cover_url))
        else:
            unknown += 1
    return found, unknown


def find() -> Dict[str, Any]:
    found, unknown = _stand_ins()
    return {"count": len(found), "no_address": unknown, "fetching": status()}


def status() -> Dict[str, Any]:
    with _lock:
        return dict(_state)


def start(user_id: str) -> Dict[str, Any]:
    with _lock:
        if _state["running"]:
            return dict(_state)
        _state.update(running=True, done=0, total=0, fixed=0, failed=0, current="")
    threading.Thread(target=_fetch, args=(user_id,), daemon=True, name="covers").start()
    return status()


def _fetch(user_id: str) -> None:
    try:
        found, _ = _stand_ins()
        with _lock:
            _state["total"] = len(found)
        for novel_id, title, url, cover_url in found:
            with _lock:
                _state["current"] = title or ""
            cover = ctx.files.resolve(f"novels/{novel_id}/cover.jpg")
            try:
                with ctx.crawler.prepare_crawler(user_id, url) as crawler:
                    crawler.download_cover(cover_url, cover)
                fixed = not is_stand_in_cover(cover)
            except Exception as error:
                logger.warning(f"Could not fetch the cover of {title}: {error}")
                fixed = False
            with _lock:
                _state["fixed" if fixed else "failed"] += 1
                _state["done"] += 1
    except Exception as error:
        logger.warning(f"Fetching covers stopped: {error}")
    finally:
        with _lock:
            _state["running"] = False
            _state["current"] = ""
