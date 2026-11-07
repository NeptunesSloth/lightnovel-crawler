from functools import lru_cache
import re
from typing import Any, List, Tuple

from bs4 import BeautifulSoup
from lxml import html as lxml_html

NOVELFIRE_LOGIN_NOTICE = (
    "Please login to use features: save the novel you are reading, "
    "save the reading position and note your favorite content."
)


def _normalized_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def has_chapter_content(body: str) -> bool:
    """Accept readable text or image pages, but not empty HTML wrappers."""
    if not body or not body.strip():
        return False
    # A chapter is checked when it is formatted, before it is saved and when it
    # is read back for gaps: the same text, so remember the answer.
    return _has_content(body)


_SKIPPED = frozenset({"script", "style", "template"})
_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})


@lru_cache(maxsize=512)
def _has_content(body: str) -> bool:
    try:
        root = lxml_html.document_fromstring(body)
    except Exception:  # lxml rejects some markup (e.g. only a comment)
        return _has_content_slow(body)
    # one walk, the same as the BeautifulSoup version below: text outside
    # script/style/template (and apart, the text outside headings too) and
    # whether any image has a source
    text: List[str] = []
    text_outside_headings: List[str] = []
    has_image = has_image_outside_headings = False
    stack: List[Tuple[Any, bool, bool]] = [(root, False, False)]
    while stack:
        element, closing, in_heading = stack.pop()
        if closing:
            if element.tail and element is not root:
                text.append(element.tail)
                if not in_heading:
                    text_outside_headings.append(element.tail)
            continue
        stack.append((element, True, in_heading))
        tag = element.tag
        if not isinstance(tag, str) or tag in _SKIPPED:
            continue  # comment, processing instruction or hidden: only its tail counts
        inner = in_heading or tag in _HEADINGS
        if element.text:
            text.append(element.text)
            if not inner:
                text_outside_headings.append(element.text)
        if tag == "img" and str(element.get("src", "")).strip():
            has_image = True
            has_image_outside_headings = has_image_outside_headings or not inner
        stack.extend((child, False, inner) for child in reversed(element))

    joined = " ".join(part.strip() for part in text if part.strip())
    joined = joined.replace("\u200b", "").replace("\ufeff", "").strip()
    notice = _normalized_text(NOVELFIRE_LOGIN_NOTICE).rstrip(".")
    if notice in _normalized_text(joined):
        # NovelFire can return only its reading-position/login notice. A
        # chapter heading beside that notice is metadata, not story content.
        rest = _normalized_text(" ".join(part.strip() for part in text_outside_headings))
        joined = rest.replace(notice, "").strip(". \t\r\n\u200b\ufeff")
        return bool(joined or has_image_outside_headings)
    return bool(joined or has_image)


def _has_content_slow(body: str) -> bool:
    """The BeautifulSoup version, for markup lxml cannot build a tree of."""
    soup = BeautifulSoup(body, "lxml")
    for element in soup.select("script, style, template"):
        element.decompose()
    text = soup.get_text(" ", strip=True).replace("\u200b", "").replace("\ufeff", "").strip()
    notice = _normalized_text(NOVELFIRE_LOGIN_NOTICE).rstrip(".")
    if notice in _normalized_text(text):
        # NovelFire can return only its reading-position/login notice. A
        # chapter heading beside that notice is metadata, not story content.
        for heading in soup.select("h1, h2, h3, h4, h5, h6"):
            heading.decompose()
        text = _normalized_text(soup.get_text(" ", strip=True))
        text = text.replace(notice, "").strip(". \t\r\n\u200b\ufeff")
    return bool(text or any(str(image.get("src", "")).strip() for image in soup.select("img[src]")))
