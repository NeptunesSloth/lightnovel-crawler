import logging
from threading import Event
from typing import Iterable, Optional

from ...context import ctx
from ...enums import LanguageCode
from ._base import BackendBase

logger = logging.getLogger(__name__)

# Readable target-language names for the prompt (the model handles plain codes
# too, but names give cleaner results). Falls back to the raw code otherwise.
_LANG_NAMES = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "pt": "Portuguese",
    "it": "Italian",
    "ru": "Russian",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
    "zh-cn": "Simplified Chinese",
    "zh-tw": "Traditional Chinese",
    "bn": "Bangla",
    "vi": "Vietnamese",
    "id": "Indonesian",
    "ar": "Arabic",
    "hi": "Hindi",
}


class OpenAILLMTranslate(BackendBase):
    """High-quality literary translation via an OpenAI-compatible chat model.

    Enabled only when an OpenAI API key is configured; otherwise the classic
    Google/Bing/Baidu engines are used. Each paragraph is translated on its own
    (no fragile separator round-tripping), with light concurrency.
    """

    def __init__(self) -> None:
        super().__init__(max_workers=4)
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI

            base_url = ctx.config.translator.llm_base_url or None
            self._client = OpenAI(api_key=ctx.config.app.openai_key, base_url=base_url)
        return self._client

    def is_enabled(self, language: LanguageCode) -> bool:
        return bool(ctx.config.app.openai_key) and bool(ctx.config.translator.llm_model)

    def _lang_name(self, target: LanguageCode) -> str:
        code = getattr(target, "value", None) or str(target)
        return _LANG_NAMES.get(code, code)

    def _translate_one(self, text: str, target: LanguageCode, signal: Optional[Event]) -> str:
        if signal is not None and signal.is_set():
            raise RuntimeError("translation aborted")
        if not text or not text.strip():
            return text
        target_name = self._lang_name(target)
        resp = self._get_client().chat.completions.create(
            model=ctx.config.translator.llm_model,
            temperature=0.2,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"You are a professional literary translator. Translate the user's "
                        f"text into {target_name}. Preserve the meaning, tone, names, and any "
                        "HTML tags exactly. Keep proper nouns and terminology consistent. "
                        "Output ONLY the translation — no notes, quotes, or preamble."
                    ),
                },
                {"role": "user", "content": text},
            ],
        )
        out = (resp.choices[0].message.content or "").strip()
        return out or text

    def translate_batch(
        self,
        texts: Iterable[str],
        target: LanguageCode,
        signal: Optional[Event] = None,
    ) -> Iterable[str]:
        text_list = list(texts)
        futures = [
            self._taskman.submit_task(self._translate_one, text, target, signal)
            for text in text_list
        ]
        self._taskman.resolve(futures, fail_fast=True, desc="AI translating", unit="para")
        for f in futures:
            yield f.result()
