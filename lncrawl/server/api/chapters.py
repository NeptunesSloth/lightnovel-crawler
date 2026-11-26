from typing import List, Optional

from fastapi import APIRouter, Path, Query, Request, Security
from fastapi.responses import Response

from ...context import ctx
from ...dao import ActivityType, Chapter, ChapterImage, Job, LanguageCode, User
from ...exceptions import ServerError, ServerErrors
from ..caching import cached_file_response
from ..models import ReadChapterResponse
from ..security import ensure_user

# The root router
router = APIRouter()


@router.get("/image/{image_id}", summary="Serve a chapter image file (for the reader)")
def serve_chapter_image(
    request: Request,
    image_id: str = Path(),
    chapter: Optional[str] = Query(default=None, description="The chapter showing it"),
    user: User = Security(ensure_user),
) -> Response:
    path = None
    try:
        image = ctx.images.get(image_id)
        path = ctx.files.resolve(image.image_file)
    except ServerError:
        pass
    if path is None or not path.is_file():
        # lost from the library, but the novel's own e-book may still hold it
        from ...services import page_rescue

        books = getattr(ctx, "books", None)
        path = None
        if chapter and not (
            books is not None and books.unavailable(ctx.chapters.get(chapter).novel_id)
        ):
            path = page_rescue.restore(image_id, chapter, user.id)
        if path is None:
            raise ServerErrors.no_such_file
    # Manga pages are re-read often; a 304 skips sending the image again.
    return cached_file_response(request, path, "image/jpeg")


@router.get("/{chapter_id}", summary="Returns a chapter details")
def get_chapter(
    chapter_id: str = Path(),
) -> Chapter:
    return ctx.chapters.get(chapter_id)


# sync on purpose: a DB query, run in the thread pool, not on the event loop
@router.get("/{chapter_id}/images", summary="Gets list of chapter images")
def get_chapter_images(
    chapter_id: str = Path(),
    available_only: bool = Query(default=False, description="List only available images"),
) -> List[ChapterImage]:
    return ctx.images.list(
        chapter_id=chapter_id,
        is_crawled=available_only,
    )


@router.get("/{chapter_id}/read", summary="Get chapter content for reading")
def read_chapter(
    chapter_id: str = Path(),
    user: User = Security(ensure_user),
    language: Optional[LanguageCode] = Query(
        default=None,
        description="Target language code, e.g. 'fr', 'zh-CN'",
    ),
    auto_fetch: Optional[bool] = Query(
        default=None,
        description="Fetch content automatically if not available",
    ),
    preload: bool = Query(
        default=False,
        description="Fetched ahead of being read: not added to the read history",
    ),
) -> ReadChapterResponse:
    ctx.activity.record(user.id, ActivityType.CHAPTER, chapter_id)
    if language:
        ctx.activity.record(user.id, ActivityType.CHAPTER_TRANSLATION, chapter_id)
    return ctx.chapters.read(
        user,
        chapter_id,
        language=language,
        auto_fetch=auto_fetch,
        add_to_history=not preload,
    )


@router.get("/{chapter_id}/fetch", summary="Create a job to fetch chapter content")
def fetch_chapter(
    user: User = Security(ensure_user),
    chapter_id: str = Path(),
) -> Job:
    job = ctx.jobs.get_chapter_job(user.id, chapter_id)
    if not job:
        job = ctx.jobs.fetch_chapter(user, chapter_id)
    return job


@router.get("/{chapter_id}/translate", summary="Create a job to translate chapter content")
def translate_chapter(
    user: User = Security(ensure_user),
    chapter_id: str = Path(),
    language: LanguageCode = Query(description="Target language code, e.g. 'fr', 'zh-CN'"),
) -> Job:
    job = ctx.jobs.get_chapter_translation_job(user.id, chapter_id, language)
    if not job:
        job = ctx.jobs.translate_chapter(user, chapter_id, language)
    return job
