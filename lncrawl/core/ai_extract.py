"""LLM-assisted structure inference for the AI universal crawler.

Given a novel page, an LLM infers the CSS selectors for the chapter list; given a
chapter page, it infers the content selector. The selectors are then applied with
BeautifulSoup for the whole novel — so the LLM is called at most twice per novel,
not once per chapter. A deterministic "readability" fallback keeps things working
if the model returns a bad selector.
"""

import json
import logging
import re
from typing import TYPE_CHECKING, Optional

from ..context import ctx

if TYPE_CHECKING:
    from scraper import PageSoup

logger = logging.getLogger(__name__)

_HTML_LIMIT = 14000  # chars of (stripped) HTML sent to the model
_STRIP = re.compile(r"<(script|style|svg|noscript)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)


def is_available() -> bool:
    """Whether AI extraction can run (an OpenAI key + model are configured)."""
    return bool(ctx.config.app.openai_key) and bool(ctx.config.translator.llm_model)


def _client():
    from openai import OpenAI

    base_url = ctx.config.translator.llm_base_url or None
    return OpenAI(api_key=ctx.config.app.openai_key, base_url=base_url)


def _trim(html: str) -> str:
    return _STRIP.sub(" ", html or "")[:_HTML_LIMIT]


def _ask_json(system: str, html: str) -> dict:
    resp = _client().chat.completions.create(
        model=ctx.config.translator.llm_model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": _trim(html)},
        ],
    )
    text = resp.choices[0].message.content or "{}"
    try:
        return json.loads(text)
    except Exception:
        logger.warning("AI extract: could not parse model JSON")
        return {}


def infer_novel_selectors(html: str) -> dict:
    """Infer CSS selectors for a novel page.

    Returns a dict with keys: ``chapter_list`` (selects each chapter link/row),
    ``chapter_title`` and ``chapter_url`` (within a row; may be empty when the row
    is itself the ``<a>``), ``novel_title``, ``novel_cover``. Empty/missing values
    fall back to the template's sensible defaults.
    """
    system = (
        "You are given the HTML of an online novel/manga page. Return ONLY a JSON "
        "object of CSS selectors that locate its data, with these keys:\n"
        '- "chapter_list": selector matching every chapter link in the table of '
        'contents (prefer the <a> tags themselves, e.g. "ul.chapters li a").\n'
        '- "chapter_title": selector for the title within one chapter row, or "" '
        "if the matched element's own text is the title.\n"
        '- "chapter_url": selector for the link within one chapter row, or "" if '
        "the matched element is itself the <a>.\n"
        '- "novel_title": selector for the novel title, or "".\n'
        '- "novel_cover": selector for the cover <img>, or "".\n'
        "Use the most specific stable selector you can. Return only valid JSON."
    )
    data = _ask_json(system, html)
    return {
        "chapter_list": str(data.get("chapter_list") or "").strip(),
        "chapter_title": str(data.get("chapter_title") or "").strip(),
        "chapter_url": str(data.get("chapter_url") or "").strip(),
        "novel_title": str(data.get("novel_title") or "").strip(),
        "novel_cover": str(data.get("novel_cover") or "").strip(),
    }


def infer_content_selector(html: str) -> str:
    """Infer the CSS selector for the main reading content on a chapter page."""
    system = (
        "You are given the HTML of a single chapter page from a novel/manga site. "
        'Return ONLY a JSON object {"content": "<css selector>"} where the selector '
        "matches the one element wrapping the chapter's reading content (the story "
        "text or the manga page images) — not navigation, comments, ads, or footer. "
        "Return only valid JSON."
    )
    data = _ask_json(system, html)
    return str(data.get("content") or "").strip()


# ----------------------------------------------------------------------------- #
# Deterministic fallback (no LLM): pick the densest text/image block
# ----------------------------------------------------------------------------- #

_SKIP_TAGS = ("nav", "header", "footer", "aside", "script", "style", "form")
_SKIP_HINT = re.compile(r"nav|menu|comment|footer|header|sidebar|advert|ads|related", re.I)


def readability_content(soup: "PageSoup") -> Optional["PageSoup"]:
    """Best-effort main-content element when no/!bad selector is available.

    Scores candidate containers by readable text length plus image count, skipping
    obvious chrome (nav/footer/comments), and returns the best one.
    """
    best = None
    best_score = 0.0
    for tag in soup.find_all(["article", "section", "div", "main"]):
        try:
            cls = tag.get("class") or ""
            if isinstance(cls, (list, tuple)):
                cls = " ".join(cls)
            tid = f"{tag.get('id') or ''} {cls}"
        except Exception:
            tid = ""
        if _SKIP_HINT.search(tid):
            continue
        text_len = len((tag.text or "").strip())
        imgs = len(tag.find_all("img"))
        # paragraphs are a strong signal of real reading content
        paras = len(tag.find_all("p"))
        score = text_len + imgs * 400 + paras * 50
        if score > best_score:
            best_score = score
            best = tag
    return best
