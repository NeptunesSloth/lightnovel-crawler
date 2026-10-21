import re

from bs4 import BeautifulSoup

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
    soup = BeautifulSoup(body, "html.parser")
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
    return bool(text or soup.select_one("img[src]"))
