"""Merge the Reader's per-user state kept on more than one device.

The Reader keeps reading positions, favorites, bookmarks and saved filter views
in the browser, so each device used to have its own. Each device now sends its
copy to the server, which keeps one merged copy per user:

- reading positions merge novel by novel, the most recently read one winning,
  so reading one novel on a phone and another on a PC loses neither;
- every other section is replaced as a whole by whichever device changed it
  last (by its ``ts``), so removing a favorite on one device removes it on all.
"""

from typing import Any, Dict

# A JSON document per user; far above what a large library's state needs.
MAX_STATE_BYTES = 2 * 1024 * 1024

# Sections synced as a whole, newest wins.
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

    positions: Dict[str, Any] = {}
    for source in (stored.get("pos"), incoming.get("pos")):
        if not isinstance(source, dict):
            continue
        for novel_id, entry in source.items():
            if not isinstance(entry, dict):
                continue
            current = positions.get(novel_id)
            if current is None or _ts(entry.get("ts")) >= _ts(current.get("ts")):
                positions[novel_id] = entry
    out["pos"] = positions

    for name in WHOLE_SECTIONS:
        old = stored.get(name)
        new = incoming.get(name)
        candidates = [s for s in (old, new) if isinstance(s, dict) and "data" in s]
        if candidates:
            out[name] = max(candidates, key=lambda s: _ts(s.get("ts")))
    return out
