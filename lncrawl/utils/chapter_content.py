from bs4 import BeautifulSoup


def has_chapter_content(body: str) -> bool:
    """Accept readable text or image pages, but not empty HTML wrappers."""
    if not body or not body.strip():
        return False
    soup = BeautifulSoup(body, "html.parser")
    for element in soup.select("script, style, template"):
        element.decompose()
    text = soup.get_text().replace("\u200b", "").replace("\ufeff", "").strip()
    return bool(text or soup.select_one("img[src]"))
