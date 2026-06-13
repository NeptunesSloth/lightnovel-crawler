import logging
from threading import Event
from typing import Optional
from urllib.parse import quote

from ...context import ctx
from ...enums import LanguageCode
from ._base import ChunkedBackendBase
from .backend_google import _LANG_MAP

logger = logging.getLogger(__name__)


class LingvaTranslate(ChunkedBackendBase):
    """Calls Google Mobile translate API internally."""

    def is_enabled(self, language: LanguageCode) -> bool:
        return language in _LANG_MAP

    def translate(
        self,
        text: str,
        target: LanguageCode,
        signal: Optional[Event] = None,
    ) -> str:
        with ctx.http.session(signal) as sess:
            data = sess.get_json(
                f"https://lingva.ml/api/v1/auto/{target}/{quote(text, safe='')}",
                timeout=60,
            )
            return data["translation"]
