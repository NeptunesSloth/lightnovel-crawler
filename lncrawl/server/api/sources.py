import json
from typing import List

from fastapi import APIRouter, Body, Path, Query, Request, Security
from fastapi.responses import StreamingResponse

from ...context import ctx
from ...dao import ActivityType, User
from ...exceptions import ServerErrors
from ..caching import Blob, blob_response, make_blob
from ..models import (
    CrawlerTestRequest,
    PRCreateRequest,
    PRResponse,
    SourceItem,
)
from ..security import ensure_user

router = APIRouter()


@router.get(
    "s",
    summary="Returns a list of supported sources",
    response_model=List[SourceItem],
)
def list_sources(
    request: Request,
    skip_rejected: bool = Query(default=False, help="Send true to skip rejected sources"),
    user: User = Security(ensure_user),
):
    ctx.activity.record(user.id, ActivityType.SOURCES, "sources")
    count = ctx.novels.list_domains()
    result = ctx.sources.list(
        include_rejected=not skip_rejected,
    )
    for item in result:
        item.total_novels = count.get(item.domain, 0)
    # The list holds this library's novel counts, so it is checked with the
    # server every time (a 304 without the 250 KB when nothing changed); kept
    # for hours, it showed the counts from before the last downloads.
    body = json.dumps(
        [item.model_dump() for item in result],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return blob_response(request, _sources_blob(body), {"Cache-Control": "private, no-cache"})


_last_sources: List[Blob] = []


def _sources_blob(body: bytes) -> Blob:
    """The answer as a Blob, compressed once while it stays the same."""
    last = _last_sources[0] if _last_sources else None
    if last is None or last.body != body:
        last = make_blob(body, "application/json")
        _last_sources[:] = [last]
    return last


@router.get(
    "/{domain}",
    summary="Get source item",
)
def get_source(domain: str) -> SourceItem:
    source = ctx.sources.get_source(domain)
    if source is None:
        raise ServerErrors.no_crawler.with_extra(domain)
    return source


@router.get(
    "/{domain}/code",
    summary="Get source crawler file content",
)
def get_source_code(domain: str) -> str:
    ctx.sources.update()
    return ctx.github.get_source_code(domain)


@router.get(
    "/{domain}/pr",
    summary="Create a GitHub PR with an edited source crawler",
)
def get_source_pr(domain: str = Path()) -> PRResponse:
    return ctx.github.fetch_source_pr(domain)


@router.post(
    "/{domain}/pr",
    summary="Create a GitHub PR with an edited source crawler",
)
def create_source_pr(
    domain: str = Path(),
    req: PRCreateRequest = Body(...),
    user: User = Security(ensure_user),
) -> PRResponse:
    return ctx.github.create_source_pr(user, domain, req)


@router.post(
    "/{domain}/test",
    summary="Test crawler source code against a novel URL (Admin only)",
)
async def test_source(
    domain: str = Path(),
    req: CrawlerTestRequest = Body(...),
    user: User = Security(ensure_user),
) -> StreamingResponse:
    # ctx.sources.update()
    code = ctx.github.get_source_code(domain)
    if code != req.content and not user.is_admin:
        return StreamingResponse(
            "<!> Modified source code can only be executed by administrators."
            " Revert your changes or contact an admin to proceed.",
            media_type="text/plain",
        )
    return StreamingResponse(
        ctx.sources.test_source(req.url, req.content),
        media_type="text/event-stream",
    )
