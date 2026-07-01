"""Persist Cloudflare browser clearance across runs.

Solving the managed challenge takes a real browser and ~10-60 seconds, but the
resulting ``cf_clearance`` cookie (+ the exact User-Agent that earned it) keeps
working for a while afterwards. Saving it per-host means a restarted export or a
new crawl on the same site rides the previous clearance instead of re-solving —
and when the cookie has expired, the site simply challenges again and the fresh
clearance overwrites the stale entry. Best-effort everywhere: any I/O problem
just means "no saved clearance".
"""

import json
import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from ..context import ctx

logger = logging.getLogger(__name__)

_FILE = "cf-clearance.json"
_TTL = 24 * 3600  # discard saved entries older than a day


def _host_of(url_or_host: str) -> str:
    return urlparse(url_or_host).hostname or url_or_host.split("/")[0]


def _load_all() -> Dict[str, Any]:
    try:
        path = ctx.files.resolve(_FILE)
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_clearance(
    url_or_host: str,
    *,
    cf_clearance: Optional[str],
    user_agent: Optional[str],
    cookies: Optional[Dict[str, str]] = None,
) -> None:
    """Remember a solved clearance for this host (no-op without a cookie)."""
    if not cf_clearance:
        return
    host = _host_of(url_or_host)
    try:
        data = _load_all()
        data[host] = {
            "cf_clearance": cf_clearance,
            "user_agent": user_agent or "",
            "cookies": dict(cookies or {}),
            "ts": int(time.time()),
        }
        path = ctx.files.resolve(_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        logger.debug(f"Saved Cloudflare clearance for {host}")
    except Exception as e:
        logger.debug(f"Could not save clearance for {host}: {e}")


def load_clearance(url_or_host: str) -> Optional[Dict[str, Any]]:
    """A fresh saved clearance for this host, or None."""
    host = _host_of(url_or_host)
    entry = _load_all().get(host)
    if not isinstance(entry, dict) or not entry.get("cf_clearance"):
        return None
    if time.time() - float(entry.get("ts") or 0) > _TTL:
        return None
    return entry
