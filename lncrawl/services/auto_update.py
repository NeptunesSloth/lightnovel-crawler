"""Check readers' novels for new chapters once a day, without the Reader open.

The Reader's "Check updates" runs in the browser, one novel at a time, and
stops when the tab closes. A reader who turns on automatic checks in the
Reader (saved with the rest of their synced Reader state) instead gets this:
once a day the server queues a refresh of the novels they chose, so new
chapters are found, and with "download" on, fetched too.
"""

import logging
from typing import Any, Dict, List

import sqlmodel as sq

from ..context import ctx
from ..dao import Novel, User
from ..utils.time_utils import current_timestamp

logger = logging.getLogger(__name__)

# A little under a day, so a check that ran late yesterday still runs today.
INTERVAL_MS = 20 * 3600 * 1000

# Never queue more than this many novels for one reader in one run.
MAX_NOVELS = 300


def _settings(user: User) -> Dict[str, Any]:
    state = (user.extra or {}).get("reader_state") or {}
    section = state.get("autoupdate") or {}
    data = section.get("data") if isinstance(section, dict) else None
    return data if isinstance(data, dict) else {}


def _chosen_novels(user: User, scope: str) -> List[str]:
    state = (user.extra or {}).get("reader_state") or {}
    if scope == "all":
        with ctx.db.session() as sess:
            return list(sess.exec(sq.select(Novel.id).limit(MAX_NOVELS)).all())
    ids: List[str] = []
    if scope in ("favorites", "both"):
        favs = (state.get("favs") or {}).get("data") or []
        ids += [str(i) for i in favs if isinstance(i, str)]
    if scope in ("reading", "both"):
        ids += [str(i) for i in (state.get("pos") or {})]
    return list(dict.fromkeys(ids))[:MAX_NOVELS]


def run(signal=None) -> int:
    """Queue the day's checks for every reader who asked for them; returns how many novels."""
    now = current_timestamp()
    queued = 0
    with ctx.db.session() as sess:
        users = list(sess.exec(sq.select(User).where(sq.col(User.is_active).is_(True))).all())
    for user in users:
        if signal is not None and signal.is_set():
            break
        settings = _settings(user)
        if not settings.get("enabled"):
            continue
        last = int((user.extra or {}).get("autoupdate_last") or 0)
        if now - last < INTERVAL_MS:
            continue

        novel_ids = _chosen_novels(user, str(settings.get("scope") or "both"))
        with ctx.db.session() as sess:
            novels = (
                list(sess.exec(sq.select(Novel).where(sq.col(Novel.id).in_(novel_ids))).all())
                if novel_ids
                else []
            )
        try:
            if novels and settings.get("download"):
                for novel in novels:
                    ctx.jobs.fetch_latest(user, novel.id)
            elif novels:
                ctx.jobs.fetch_many_novels(user, *(n.url for n in novels), full=False)
        except Exception:
            logger.warning(f"Automatic update check failed for {user.email}", exc_info=True)
            continue
        queued += len(novels)

        with ctx.db.session() as sess:
            current = sess.get(User, user.id)
            if current:
                extra = dict(current.extra or {})
                extra["autoupdate_last"] = now
                current.extra = extra
                sess.add(current)
                sess.commit()
        if novels:
            logger.info(f"Queued a new-chapter check of {len(novels)} novel(s) for {user.email}")
    return queued
