"""Work out whether a novel or comic is finished, from whatever its source says.

Few sources hand this over as a field. Most print it on the novel page next to a
"Status" label, some file it among the tags, and some only mark the last chapter
"(The End)". Each is read here and brought to one of four values, so the library
can filter on it whatever the source called it.
"""

import re
from typing import Any, Iterable, Optional

from bs4 import Tag

COMPLETED = "completed"
ONGOING = "ongoing"
HIATUS = "hiatus"
CANCELLED = "cancelled"

# Words sources use for each state, matched whole and ignoring case.
_WORDS = {
    COMPLETED: (
        "completed",
        "complete",
        "finished",
        "ended",
        "end",
        "concluded",
        "完结",
        "完結",
        "已完结",
        "완결",
        "terminado",
        "concluído",
        "completo",
        "fini",
        "terminé",
        "tamat",
        "завершено",
        "завершён",
    ),
    ONGOING: (
        "ongoing",
        "on going",
        "on-going",
        "publishing",
        "releasing",
        "updating",
        "serializing",
        "serialization",
        "in progress",
        "连载",
        "連載",
        "连载中",
        "연재",
        "em andamento",
        "en curso",
        "en cours",
        "berlangsung",
        "продолжается",
    ),
    HIATUS: ("hiatus", "on hiatus", "on hold", "paused", "pause"),
    CANCELLED: ("cancelled", "canceled", "dropped", "discontinued", "abandoned", "axed"),
}
_LOOKUP = {word: state for state, words in _WORDS.items() for word in words}
# Too common in other phrases to decide a status unless they stand alone.
_EXACT_ONLY = {"end", "full", "complete", "pause"}

_RE_LABEL = re.compile(r"^\s*(status|novel status|series status|publication)\s*:?\s*$", re.I)
_RE_INLINE = re.compile(r"^\s*status\s*:\s*(\S.{0,38})$", re.I)
_RE_LAST_CHAPTER = re.compile(
    r"[\[(]\s*(the\s+)?end\s*[\])]|\bfinal\s+chapter\b|\(fin\)|完结|完結", re.I
)


def normalize(value: Any, loose: bool = True) -> Optional[str]:
    """One of the four states for a status a source printed, or None.

    ``loose`` also reads a status out of a longer phrase ("Completed (Scan)");
    tags are matched whole instead, so "Full Color" is not read as finished.
    """
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value).strip().strip(".:;,").lower()
    if not text or len(text) > 40:
        return None
    if text in _LOOKUP:
        return _LOOKUP[text]
    if not loose:
        return None
    # "Completed (Scan)", "Status: Ongoing": the first known word decides
    for word in re.findall(r"[^\W\d_]+(?:[ -][^\W\d_]+)?", text):
        for candidate in (word, word.split(" ")[0].split("-")[0]):
            if candidate in _LOOKUP and candidate not in _EXACT_ONLY:
                return _LOOKUP[candidate]
    return None


def from_tags(tags: Iterable[str]) -> Optional[str]:
    """A status filed among a novel's tags ("Completed", "Ongoing")."""
    for tag in tags or []:
        state = normalize(tag, loose=False)
        if state:
            return state
    return None


def from_page(root: Any) -> Optional[str]:
    """A status printed beside a "Status" label on a novel page.

    Covers the common layouts: the value in the next element (or the one
    before, as some print "Ongoing" over a small "Status" caption), in the
    label's parent after the label, or in the next cell of a two-column summary.
    """
    # a scraper PageSoup wraps the tag; a bs4 Tag would read `.tag` as a search
    tag = root if isinstance(root, Tag) else getattr(root, "tag", None)
    if not isinstance(tag, Tag):
        return None
    for label in tag.find_all(string=_RE_LABEL):
        node = label.parent
        for _ in range(3):  # the label, then up to two wrapping elements
            if node is None:
                break
            for sibling in (node.find_next_sibling(), node.find_previous_sibling()):
                if sibling is not None:
                    state = normalize(sibling.get_text(" ", strip=True))
                    if state:
                        return state
            parent_text = node.parent.get_text(" ", strip=True) if node.parent else ""
            state = normalize(_RE_LABEL.sub("", parent_text.replace(str(label).strip(), "", 1)))
            if state:
                return state
            node = node.parent
    # "Status: Completed" as one run of text
    for text in tag.find_all(string=_RE_INLINE):
        match = _RE_INLINE.match(str(text))
        state = normalize(match.group(1)) if match else None
        if state:
            return state
    return None


def detect(
    status: Any = None, tags: Iterable[str] = (), last_chapter_title: str = ""
) -> Optional[str]:
    """The best guess at a status, from the strongest evidence available."""
    return (
        normalize(status)
        or from_tags(tags)
        or (
            COMPLETED
            if last_chapter_title and _RE_LAST_CHAPTER.search(last_chapter_title)
            else None
        )
    )
