import os
import shutil
import threading
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Path, Query, Request, Security
from fastapi.responses import Response
import sqlmodel as sq

from ...context import ctx
from ...dao import ActivityType, Artifact, Chapter, Job, LanguageCode, Novel, User, Volume
from ...exceptions import ServerError, ServerErrors
from ...utils.file_tools import dir_size
from ..caching import cached_file_response
from ..models import Paginated
from ..models.novel import NovelCompleteness
from ..security import ensure_admin, ensure_user

# The root router
router = APIRouter()


@router.get("/{novel_id}/completeness", summary="Downloaded, missing, failed and empty chapters")
def novel_completeness(novel_id: str = Path()) -> NovelCompleteness:
    ctx.novels.get(novel_id)
    return ctx.chapters.completeness(novel_id)


@router.post("/{novel_id}/repair", summary="Retry chapter gaps and rebuild a complete EPUB")
def repair_novel(
    novel_id: str = Path(),
    user: User = Security(ensure_user),
) -> Job:
    return ctx.jobs.fetch_missing_chapters(user, novel_id, rebuild_epub=True)


@router.post(
    "/{novel_id}/ebook",
    summary="This novel's e-book with every chapter it has, made now if need be",
)
def novel_ebook(novel_id: str = Path()) -> Artifact:
    from ...services.library_books import book_for

    ctx.novels.get(novel_id)
    books = getattr(ctx, "books", None)
    if books is not None and books.unavailable(novel_id):
        raise ServerError(503, f"This novel is on the book drive ({books.folder()}): plug it in")
    try:
        return book_for(novel_id)
    except ValueError as error:
        raise ServerError(400, str(error)) from error


@router.get("/{novel_id}/cover", summary="Serve the novel cover image (for the reader)")
def novel_cover(
    request: Request,
    novel_id: str = Path(),
) -> Response:
    novel = ctx.novels.get(novel_id)
    path = ctx.files.resolve(novel.cover_file)
    if not path.is_file():
        raise ServerErrors.no_such_file
    # The Reader asks for every cover again on each visit; a 304 skips the body.
    return cached_file_response(request, path, "image/jpeg")


@router.get(
    "s",
    summary="Returns a list of novels",
)
def list_novels(
    search: str = Query(default="", help="Search query"),
    offset: int = Query(default=0, help="Offset"),
    limit: int = Query(default=20, le=100, help="Limit"),
    domain: str = Query(default="", help="Domain name"),
) -> Paginated[Novel]:
    return ctx.novels.list(
        limit=limit,
        offset=offset,
        search=search.strip(),
        domain=domain.strip(),
    )


@router.get(
    "/domains",
    summary="Returns a list of sources that are used in available novels",
)
def list_sources() -> Dict[str, int]:
    return ctx.novels.list_domains()


@router.get(
    "s/latest-chapters",
    summary="When each novel last gained a chapter, as {novel_id: timestamp}",
)
def latest_chapters() -> Dict[str, int]:
    """The newest chapter's creation time per novel.

    A chapter row is created when an update first finds it, so this is when the
    novel last got something new to read; the novel's own ``updated_at`` moves
    on every refresh, new chapters or not.

    Asked per novel, done and not done apart, so each is one look into the
    (novel_id, is_done, created_at) index: grouping every chapter scanned the
    whole index, a tenth of a second with a million chapters.
    """

    def newest(done: bool) -> Any:
        return (
            sq.select(sq.func.max(Chapter.created_at))
            .where(sq.col(Chapter.novel_id) == sq.col(Novel.id), sq.col(Chapter.is_done).is_(done))
            .scalar_subquery()
        )

    with ctx.db.session() as sess:
        rows = sess.exec(
            sq.select(Novel.id, newest(False), newest(True)).where(
                sq.exists().where(sq.col(Chapter.novel_id) == sq.col(Novel.id))
            )
        ).all()
    return {novel_id: int(max(pending or 0, done or 0)) for novel_id, pending, done in rows}


@router.get(
    "s/downloaded-counts",
    summary="How many chapters each novel has downloaded, as {novel_id: count}",
)
def downloaded_counts() -> Dict[str, int]:
    """Downloaded chapters per novel, in one grouped query.

    A novel missing from the result has none: a discovery that added only the
    details, or a download that never got going. The Reader marks those so
    their chapters can be fetched.
    """
    with ctx.db.session() as sess:
        rows = sess.exec(
            sq.select(Chapter.novel_id, sq.func.count())
            .where(sq.col(Chapter.is_done).is_(True))
            .group_by(sq.col(Chapter.novel_id))
        ).all()
    return {novel_id: int(count or 0) for novel_id, count in rows}


@router.get(
    "s/unfinished",
    summary="Novels not fully downloaded, and how a removal of them is going",
    dependencies=[Security(ensure_admin)],
)
def unfinished_novels() -> Dict[str, Any]:
    from ...services import unfinished

    found = unfinished.find()
    return {
        "count": len(found["novels"]),
        "details_only": found["details_only"],
        "partial": found["partial"],
        "chapters": found["chapters"],
        "kept": found["kept"],
        "removing": unfinished.status(),
    }


@router.post(
    "s/unfinished/remove",
    summary="Remove every novel not fully downloaded, except those a download still uses",
    dependencies=[Security(ensure_admin)],
)
def remove_unfinished_novels(
    kind: str = Query(default="all", description="details, partial or all"),
) -> Dict[str, Any]:
    from ...services import unfinished

    try:
        return unfinished.start(kind)
    except ValueError as error:
        raise ServerError(400, str(error)) from error


# What uses the space, worked out in the background and kept: going through
# every novel's folder is 1.6 million files on a big library (minutes to hours
# on an external drive), and it was done again each time the Tools page was
# opened, however many were already running.
_storage_lock = threading.Lock()
_storage: Dict[str, Any] = {"measuring": False, "measured_at": None, "result": None}


def _measure_storage() -> None:
    try:
        sizes: Dict[str, int] = {}
        folders = [ctx.files.resolve("novels")]
        drive = ctx.books.folder() if ctx.books.plugged() else None
        if drive is not None:
            folders.append(drive / "novels")  # books kept on a drive of their own
        for novels_dir in folders:
            if not novels_dir.is_dir():
                continue
            for child in os.scandir(novels_dir):
                if child.is_dir(follow_symlinks=False):
                    # Allocated blocks, not the apparent byte sum: this view answers
                    # "how full is the disk", and it is the measure the scrubber
                    # decides by when it starts deleting to make room.
                    sizes[child.name] = sizes.get(child.name, 0) + dir_size(
                        child.path, allocated=True
                    )
        largest = sorted(sizes.items(), key=lambda kv: -kv[1])[:15]
        # Titles for exactly the rows shown, not the newest 100 novels.
        titles = ctx.novels.titles_by_id([nid for nid, _ in largest])
        result = {
            "total": sum(sizes.values()),
            "exports": dir_size(ctx.files.resolve("exports"), allocated=True),
            "novel_count": len(sizes),
            "largest": [
                {"id": nid, "title": titles.get(nid, nid), "size": size} for nid, size in largest
            ],
        }
        with _storage_lock:
            _storage.update(result=result, measured_at=int(time.time()))
    finally:
        with _storage_lock:
            _storage["measuring"] = False


@router.get("s/storage", summary="Disk usage of the downloaded library and exports")
def novels_storage(
    measure: bool = Query(default=False, description="Work it out again, in the background"),
) -> Dict[str, Any]:
    """The last measurement, at once; with ``measure`` a new one is started."""
    with _storage_lock:
        if measure and not _storage["measuring"]:
            _storage["measuring"] = True
            threading.Thread(target=_measure_storage, daemon=True, name="storage").start()
        result = dict(_storage["result"] or {})
        result.update(measuring=_storage["measuring"], measured_at=_storage["measured_at"])
    try:
        usage = shutil.disk_usage(ctx.config.app.app_dir)
        result.update(free=usage.free, disk_total=usage.total)
    except OSError:
        pass
    return result


@router.delete("s/storage/exports", summary="Delete generated export zips to free disk space")
def cleanup_exports() -> Dict[str, int]:
    exports = ctx.files.resolve("exports")
    removed = 0
    freed = 0
    if exports.is_dir():
        for f in exports.glob("*.zip"):
            if f.name.startswith("backup-"):
                continue  # never touch backups here
            try:
                freed += f.stat().st_size
                f.unlink()
                removed += 1
            except OSError:
                continue
    return {"removed": removed, "freed": freed}


@router.get("/{novel_id}", summary="Returns a novel")
def get_novel(
    novel_id: str = Path(),
    language: Optional[LanguageCode] = Query(default=None),
    user: User = Security(ensure_user),
) -> Novel:
    ctx.activity.record(user.id, ActivityType.NOVEL, novel_id)
    if language:
        ctx.activity.record(user.id, ActivityType.NOVEL_TRANSLATION, novel_id)
    return ctx.novels.get(novel_id, language)


@router.post(
    "/{novel_id}/refresh",
    summary="Re-fetch novel info from the source (synopsis, tags, cover, new chapters)",
)
def refresh_novel(
    novel_id: str = Path(),
    user: User = Security(ensure_user),
) -> Novel:
    novel = ctx.novels.get(novel_id)
    return ctx.crawler.fetch_novel(user.id, novel.url)


@router.post(
    "/{novel_id}/merge",
    summary="Merge all same-title copies into the most complete one (deletes the others)",
)
def merge_novel(
    novel_id: str = Path(),
    user: User = Security(ensure_user),
) -> Dict[str, object]:
    # merging deletes the other copies: only for someone who may delete them
    if not ctx.novels.can_delete(novel_id, user):
        raise ServerErrors.cannot_delete_novel
    return ctx.novels.merge_duplicates(novel_id)


@router.post(
    "/{novel_id}/heal",
    summary="Fill missing chapters from another copy of the same novel in the library",
)
def heal_novel(
    novel_id: str = Path(),
    user: User = Security(ensure_user),
) -> Dict[str, object]:
    return ctx.novels.heal_from_library(novel_id)


@router.post(
    "/{novel_id}/heal-deep",
    summary="Create a job that searches other sites for this novel's missing chapters",
)
def heal_novel_deep(
    novel_id: str = Path(),
    user: User = Security(ensure_user),
) -> Job:
    return ctx.jobs.heal_novel(user, novel_id)


@router.get("/{novel_id}/languages", summary="Gets available translation languages")
def get_novel_languages(
    novel_id: str = Path(),
) -> List[LanguageCode]:
    return ctx.novels.list_translation_languages(novel_id)


# The listings below are plain (sync) handlers on purpose: they run DB queries,
# which FastAPI then runs in its thread pool instead of on the event loop.
@router.get("/{novel_id}/volumes", summary="Gets volumes")
def get_novel_volumes(
    novel_id: str = Path(),
    language: Optional[LanguageCode] = Query(default=None),
) -> List[Volume]:
    return ctx.volumes.list(novel_id, language)


@router.get("/{novel_id}/chapters", summary="Gets all chapters")
def get_novel_chapters(
    novel_id: str = Path(),
    offset: int = Query(default=0),
    limit: int = Query(default=20, le=100),
    language: Optional[LanguageCode] = Query(default=None),
) -> Paginated[Chapter]:
    return ctx.chapters.list_page(
        limit=limit,
        offset=offset,
        novel_id=novel_id,
        language=language,
    )


@router.get("/{novel_id}/artifacts", summary="Gets latest artifacts")
def get_novel_artifacts(
    novel_id: str = Path(),
    language: Optional[LanguageCode] = Query(default=None),
) -> List[Artifact]:
    return ctx.artifacts.list_latest(novel_id, language)


@router.get("/{novel_id}/recommended", summary="Gets recommended novels based on similarity")
def get_novel_recommended(
    novel_id: str = Path(),
    limit: int = Query(default=8, ge=4, le=20),
) -> List[Novel]:
    return ctx.recommendations.get(novel_id, limit)


@router.delete(
    "/{novel_id}",
    summary="Removes a novel (admins: any; others: ones only they imported or made)",
)
def delete_novel(
    novel_id: str = Path(),
    user: User = Security(ensure_user),
) -> bool:
    if not ctx.novels.can_delete(novel_id, user):
        raise ServerErrors.cannot_delete_novel
    ctx.novels.delete(novel_id)
    return True
