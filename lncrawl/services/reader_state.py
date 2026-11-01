"""Merge the Reader's per-user state kept on more than one device.

The Reader keeps reading positions, favorites, bookmarks and saved filter views
in the browser, so each device used to have its own. Each device now sends its
copy to the server, which keeps one merged copy per user:

- keyed sections merge entry by entry, the most recent change to each entry
  winning: reading positions (per novel), favorites, bookmarks and ratings (per
  item, a removal kept as an entry marked off so it travels too), and reading
  stats (per device and day, so two devices' counts add up rather than replace
  each other). Changing different items on two devices loses neither;
- the remaining sections (settings, saved views) are replaced as a whole by
  whichever device changed them last (by their ``ts``).
"""

from typing import Any, Dict

# A JSON document per user; far above what a large library's state needs.
MAX_STATE_BYTES = 2 * 1024 * 1024

# Sections merged entry by entry, each entry carrying its own ``ts``.
KEYED_SECTIONS = ("pos", "favitems", "bmkitems", "rateitems", "stats")

# Sections synced as a whole, newest wins. favs, bookmarks and ratings are the
# copies older Readers sent before their keyed forms existed.
WHOLE_SECTIONS = ("favs", "bookmarks", "views", "prefs", "updates", "ratings", "autoupdate")


def _ts(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def merge(stored: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    """The state that results from ``incoming`` arriving on top of ``stored``."""
    stored = stored if isinstance(stored, dict) else {}
    incoming = incoming if isinstance(incoming, dict) else {}
    out: Dict[str, Any] = {}

    for name in KEYED_SECTIONS:
        entries: Dict[str, Any] = {}
        for source in (stored.get(name), incoming.get(name)):
            if not isinstance(source, dict):
                continue
            for key, entry in source.items():
                if not isinstance(entry, dict):
                    continue
                current = entries.get(key)
                if current is None or _ts(entry.get("ts")) >= _ts(current.get("ts")):
                    entries[key] = entry
        if entries or name == "pos":
            out[name] = entries

    for name in WHOLE_SECTIONS:
        old = stored.get(name)
        new = incoming.get(name)
        candidates = [s for s in (old, new) if isinstance(s, dict) and "data" in s]
        if candidates:
            out[name] = max(candidates, key=lambda s: _ts(s.get("ts")))
    return out
