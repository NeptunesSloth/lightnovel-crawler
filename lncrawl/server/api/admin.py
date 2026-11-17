import os
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile
import time
from typing import Any, Dict, List, Literal, Optional, Union
import zipfile

from fastapi import APIRouter, Body, Query
from fastapi.responses import FileResponse
from sqlalchemy.engine import make_url
from starlette.background import BackgroundTask

from ...context import ctx
from ...exceptions import ServerErrors
from ...utils.file_tools import writable_folder
from ..models import (
    ConfigSection,
    ConfigUpdateRequest,
)
from ..models.activity import (
    DailyActiveUsers,
    DailyTypeCount,
    GlobalActivitySummary,
    TopUserActivity,
)
from ..models.library import LibraryExportRequest, LibraryMoveRequest

# The root router
router = APIRouter()


def _build_library(
    output: Path,
    nsfw: Literal["include", "exclude", "only"],
    complete_only: bool,
    left_out: List[str],
) -> int:
    """Write the library zip to ``output``; the number of books in it."""
    from ...dao import Novel
    from ...services.library_export import build_library_zip

    def tags_for(novel_id: str) -> List[str]:
        with ctx.db.session() as sess:
            novel = sess.get(Novel, novel_id)
            return list(novel.tags) if novel else []

    def full_length(novel_id: Optional[str], url: Optional[str]) -> Optional[int]:
        if not novel_id and url:
            novel = ctx.novels.find_by_url(url)
            novel_id = novel.id if novel else None
        if not novel_id:
            return None
        report = ctx.chapters.completeness(novel_id)
        return report.total if report.complete else None

    return build_library_zip(
        ctx.files.resolve("."),
        output,
        nsfw=nsfw,
        tags_for=tags_for,
        full_length=full_length if complete_only else None,
        left_out=left_out,
    )


@router.get(
    "/library/export",
    summary="Download all saved EPUBs and CBZs with an offline index, one copy per novel",
)
def export_library(
    nsfw: Literal["include", "exclude", "only"] = Query(
        default="include",
        description="Keep NSFW novels (include), leave them out (exclude), or export only them",
    ),
    complete_only: bool = Query(
        default=False,
        description="Only novels with every chapter downloaded, in a book that holds them all",
    ),
) -> FileResponse:
    # Built in the data folder, which can be moved to a roomier drive: the
    # system's temp folder is on the system drive, and a library zip is about
    # as big as every book in the library.
    exports = ctx.files.resolve("exports")
    exports.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=exports, prefix="library-", suffix=".zip", delete=False) as temp:
        output = Path(temp.name)
    try:
        left_out: List[str] = []
        count = _build_library(output, nsfw, complete_only, left_out)
        if not count:
            raise ServerErrors.no_books_to_export
        return FileResponse(
            output,
            media_type="application/zip",
            filename="library.zip",
            headers={"X-Books": str(count), "X-Left-Out": str(len(left_out))},
            background=BackgroundTask(output.unlink, missing_ok=True),
        )
    except BaseException:
        output.unlink(missing_ok=True)
        raise


@router.post(
    "/library/export",
    summary="Write all saved EPUBs and CBZs with an offline index straight to a folder",
)
def export_library_to_folder(body: LibraryExportRequest = Body()) -> Dict[str, Any]:
    """The library zip, written into a folder chosen on this machine and nowhere else."""
    try:
        folder = writable_folder(body.save_dir)
    except (OSError, ValueError) as error:
        raise ServerErrors.invalid_input.with_extra(
            f"Cannot save the zip to {body.save_dir}: {error}"
        ) from error
    output = folder / "library.zip"
    counter = 1
    while output.exists():  # an earlier library zip there is never replaced
        output = folder / f"library-{time.strftime('%Y%m%d-%H%M%S')}-{counter}.zip"
        counter += 1
    # written under a temporary name: a half-written zip never looks finished
    writing = output.with_name(f"{output.name}.part")
    left_out: List[str] = []
    try:
        count = _build_library(writing, body.nsfw, body.complete_only, left_out)
        if not count:
            raise ServerErrors.no_books_to_export
        os.replace(writing, output)
    except BaseException:
        writing.unlink(missing_ok=True)
        raise
    return {
        "file": str(output),
        "size": output.stat().st_size,
        "books": count,
        "left_out": len(left_out),
        "left_out_titles": sorted(left_out, key=str.lower)[:50],
    }


@router.get("/library/location", summary="Where the library is, and any move under way")
def library_location() -> Dict[str, Any]:
    return ctx.library_move.status()


@router.post("/library/move", summary="Move the whole library to a folder on another drive")
def move_library(body: LibraryMoveRequest = Body()) -> Dict[str, Any]:
    """Copy the library to ``folder`` while downloads are paused.

    The library stays in use where it is until the copy is complete; the next
    start of the app switches to the new folder and removes the old copy.
    """
    try:
        return ctx.library_move.start(body.folder.strip())
    except (OSError, ValueError) as error:
        raise ServerErrors.invalid_input.with_extra(
            f"Cannot move the library to {body.folder}: {error}"
        ) from error


@router.delete("/library/move", summary="Cancel a move; the library stays where it is")
def cancel_library_move() -> Dict[str, Any]:
    return ctx.library_move.cancel()


@router.post("/backup", summary="Create a full backup zip (database + downloaded library)")
def create_backup() -> Dict[str, Any]:
    """Zip a consistent snapshot of the database plus the whole novels library.

    Stored (not compressed) for speed — chapter JSON and JPEGs barely compress.
    The zip lands in the exports folder and is also copied to the Desktop when
    one exists. Restore = extract into the data folder while the app is closed.
    """
    from ...services.scheduler.handlers.export_source import _copy_to_desktop

    exports = ctx.files.resolve("exports")
    exports.mkdir(parents=True, exist_ok=True)
    name = f"backup-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    out = exports / name

    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:
        # a consistent DB snapshot via SQLite's online backup API (safe mid-run)
        if ctx.db.engine.dialect.name == "sqlite":
            db_path = make_url(ctx.config.db.url).database
            if db_path:
                tmp = out.with_suffix(".db-snapshot")
                src = sqlite3.connect(db_path)
                dst = sqlite3.connect(str(tmp))
                try:
                    with dst:
                        src.backup(dst)
                finally:
                    src.close()
                    dst.close()
                zf.write(tmp, arcname=f"database/{Path(db_path).name}")
                tmp.unlink()
        # every downloaded novel: chapters, images, covers
        novels_dir = ctx.files.resolve("novels")
        if novels_dir.is_dir():
            for f in novels_dir.rglob("*"):
                if f.is_file():
                    zf.write(f, arcname=f"novels/{f.relative_to(novels_dir).as_posix()}")

    size = out.stat().st_size
    saved_to = _copy_to_desktop(out, name)
    return {"file": name, "size": size, "saved_to": saved_to}


@router.get("/backup/file", summary="Download the most recent backup zip")
def download_backup() -> FileResponse:
    exports = ctx.files.resolve("exports")
    backups = sorted(exports.glob("backup-*.zip")) if exports.is_dir() else []
    if not backups:
        raise ServerErrors.no_such_file
    latest = backups[-1]
    return FileResponse(latest, media_type="application/zip", filename=latest.name)


# sync on purpose: downloads from the repository, so it runs in the thread pool
@router.post("/update-sources", summary="Update sources from the repository")
def update() -> int:
    return ctx.admin.update_sources()


@router.post("/soft-restart", summary="Reload application context and restart the scheduler")
def soft_restart() -> None:
    ctx.admin.soft_restart()


@router.get("/runner/status", summary="Get runner status")
def status() -> bool:
    return bool(ctx.scheduler.running)


@router.post("/runner/start", summary="Start the runner")
def start() -> bool:
    ctx.scheduler.start()
    return True


@router.post("/runner/stop", summary="Stops the runner")
def stop() -> bool:
    ctx.scheduler.stop()
    return True


@router.get(
    "/configs",
    summary="List application configs",
)
def list_configs() -> List[ConfigSection]:
    return ctx.admin.config_sections()


@router.patch(
    "/configs",
    summary="Update application configs",
)
def patch_configs(
    body: List[ConfigUpdateRequest] = Body(...),
) -> None:
    ctx.admin.update_config(body)


ActivityDataType = Literal["summary", "dau", "type-trend", "top-users"]


@router.get("/activity", summary="Get admin activity dashboard data", response_model=None)
def get_activity_data(
    type: ActivityDataType = Query(..., description="Which dataset to return"),
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=20, ge=1, le=100),  # only used when type="top-users"
) -> Union[
    GlobalActivitySummary, List[DailyActiveUsers], List[DailyTypeCount], List[TopUserActivity]
]:
    if type == "summary":
        return ctx.activity.get_admin_summary(days)
    elif type == "dau":
        return ctx.activity.get_admin_dau(days)
    elif type == "type-trend":
        return ctx.activity.get_admin_type_trend(days)
    else:
        return ctx.activity.get_admin_top_users(days, limit)
