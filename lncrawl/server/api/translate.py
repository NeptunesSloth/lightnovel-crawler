from typing import Dict

from fastapi import APIRouter, Body, Security

from ...context import ctx
from ...dao import LanguageCode, User
from ...exceptions import ServerErrors
from ..security import ensure_user

# The root router
router = APIRouter()


@router.post("", summary="Translate a short text snippet (tap-to-translate in the reader)")
def translate_snippet(
    text: str = Body(embed=True, max_length=500),
    target: LanguageCode = Body(default=LanguageCode.english, embed=True),
    user: User = Security(ensure_user),
) -> Dict[str, str]:
    if not ctx.tier.translation_enabled(user):
        raise ServerErrors.tier_not_allowed
    text = text.strip()
    if not text:
        return {"translation": ""}
    return {"translation": ctx.translator.translate_text(text, target)}
