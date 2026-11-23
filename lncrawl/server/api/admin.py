import os
from pathlib import Path
import shutil
import sqlite3
from tempfile import NamedTemporaryFile
from threading import Event
import time
from typing import Any, Callable, Dict, List, Literal, Optional, Union
import zipfile

from fastapi import APIRouter, Body, Query, Security
from fastapi.responses import FileResponse
from sqlalchemy.engine import make_url
from starlette.background import BackgroundTask

from ...context import ctx
from ...dao import User
from ...exceptions import ServerError, ServerErrors
from ...utils.file_tools import format_size, writable_folder
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
from ..models.library import (
    ImportFileRequest,
    LibraryExportRequest,
    LibraryMoveRequest,
    LibraryUseRequest,
    LibraryZipRequest,
    NewFolderRequest,
)
from ..security import ensure_admin

# The root router
router = APIRouter()


def _build_library(
    output: Path,
    nsfw: Literal["include", "exclude", "only"],
    complete_only: bool,
    left_out: List[str],
    progress: Optional[Callable[..., None]] = None,
    stop: Optional[Event] = None,
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

    from ...services.library_books import make_missing_books

    # a library-only download makes no e-books: without this the ZIP held only
    # the novels that happened to have one (189 of 571)
    made, failed = make_missing_books(complete_only, progress, stop)
    if made or failed:
        ctx.logger.info(f"Library ZIP: made {made} e-book(s), {failed} could not be made")
    drive = ctx.books.folder() if ctx.books.plugged() else None
    return build_library_zip(
        ctx.files.resolve("."),
        output,
        more_novels=(drive / "novels",) if drive is not None else (),
        nsfw=nsfw,
        tags_for=tags_for,
        full_length=full_length if complete_only else None,
        left_out=left_out,
        progress=progress,
        stop=stop,
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
    return _zip_into(_zip_folder(body.save_dir), body.nsfw, body.complete_only)


def _zip_folder(save_dir: Optional[str]) -> Path:
    """The folder to write the library zip in: the one chosen, else the data folder's exports."""
    if not save_dir:
        exports = ctx.files.resolve("exports")
        exports.mkdir(parents=True, exist_ok=True)
        return exports
    try:
        return writable_folder(save_dir)
    except (OSError, ValueError) as error:
        raise ServerErrors.invalid_input.with_extra(
            f"Cannot save the zip to {save_dir}: {error}"
        ) from error


def _zip_into(
    folder: Path,
    nsfw: Literal["include", "exclude", "only"],
    complete_only: bool,
    progress: Optional[Callable[..., None]] = None,
    stop: Optional[Event] = None,
) -> Dict[str, Any]:
    output = folder / "library.zip"
    counter = 1
    while output.exists():  # an earlier library zip there is never replaced
        output = folder / f"library-{time.strftime('%Y%m%d-%H%M%S')}-{counter}.zip"
        counter += 1
    # written under a temporary name: a half-written zip never looks finished
    writing = output.with_name(f"{output.name}.part")
    left_out: List[str] = []
    try:
        count = _build_library(writing, nsfw, complete_only, left_out, progress, stop)
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


@router.post("/library/zip", summary="Start making the library ZIP in the background")
def start_library_zip(body: LibraryZipRequest = Body()) -> Dict[str, Any]:
    """Like `POST /library/export`, without waiting for it: follow it with `GET /library/zip`.

    With no folder given, the ZIP is kept in the data folder's exports, to be
    fetched from `GET /library/zip/file` when it is done.
    """
    folder = _zip_folder((body.save_dir or "").strip() or None)

    def work(progress: Callable[..., None], stop: Event) -> Dict[str, Any]:
        return _zip_into(folder, body.nsfw, body.complete_only, progress, stop)

    try:
        return ctx.library_zip.start(work)
    except ValueError as error:
        raise ServerErrors.invalid_input.with_extra(str(error)) from error


@router.get("/library/zip", summary="How far the library ZIP has got")
def library_zip_status() -> Dict[str, Any]:
    return ctx.library_zip.status()


@router.delete("/library/zip", summary="Stop making the library ZIP")
def stop_library_zip() -> Dict[str, Any]:
    return ctx.library_zip.stop()


@router.get("/library/zip/file", summary="Download the library ZIP last made in the data folder")
def library_zip_file() -> FileResponse:
    status = ctx.library_zip.status()
    file = Path(str(status.get("file") or ""))
    exports = ctx.files.resolve("exports")
    if status.get("state") != "done" or file.parent != exports or not file.is_file():
        raise ServerErrors.no_such_file
    return FileResponse(file, media_type="application/zip", filename="library.zip")


@router.get("/library/location", summary="Where the library is, and any move under way")
def library_location() -> Dict[str, Any]:
    return ctx.library_move.status()


# Room wanted on the library's drive for an import, per byte of the file: each
# book is kept as it came as well as read into chapters and pages.
IMPORT_ROOM = 2.0


@router.post("/import-file", summary="Import a ZIP or e-book from this computer, where it is")
def import_file(
    body: ImportFileRequest = Body(),
    user: User = Security(ensure_admin),
) -> Any:
    """Import a file already on this computer without uploading it.

    An upload of a library ZIP of hundreds of gigabytes was copied twice on its
    way in (by the web server to the system's temp folder, then into the
    library) and refused past 8 GB. This reads the file where it is, and leaves
    it there.
    """
    from ...services.importer import format_of

    file = Path(body.path.strip()).expanduser()
    if not file.is_absolute() or not file.is_file():
        raise ServerErrors.invalid_input.with_extra(f"{file} cannot be found")
    if file.suffix.lower() != ".zip" and format_of(file.name) is None:
        raise ServerErrors.invalid_input.with_extra(f"{file.name} is not a ZIP or an e-book")
    size = file.stat().st_size
    library = ctx.config.app.app_dir
    try:
        free = shutil.disk_usage(library).free
    except OSError:
        free = None
    need = int(size * IMPORT_ROOM) + 1024 * 1024 * 1024
    if free is not None and free < need:
        raise ServerErrors.invalid_input.with_extra(
            f"The library's drive ({library}) has {format_size(free)} free, and importing "
            f"{file.name} ({format_size(size)}) needs about {format_size(need)}. Move the "
            "library to a bigger drive first (Library and drives): an empty one moves in moments"
        )
    return ctx.jobs.import_archive(
        user,
        archive_file=str(file),
        archive_name=file.name,
        archive_size=size,
        keep_archive=True,
    )


@router.get("/folders", summary="The folders in a folder on this computer, to pick one")
def list_folders(
    path: Optional[str] = Query(default=None, description="Blank: the drives and own folders"),
    files: Optional[str] = Query(
        default=None, description="List files ending so as well, e.g. .zip,.epub"
    ),
) -> Dict[str, Any]:
    from ...utils.folders import list_folder

    suffixes = tuple(s.strip() for s in (files or "").split(",") if s.strip())
    try:
        return list_folder(path, suffixes)
    except ValueError as error:
        raise ServerErrors.invalid_input.with_extra(str(error)) from error


@router.post("/folders", summary="Make a folder on this computer")
def make_folder(body: NewFolderRequest = Body()) -> Dict[str, Any]:
    from ...utils.folders import new_folder

    try:
        return {"path": new_folder(body.path, body.name)}
    except ValueError as error:
        raise ServerErrors.invalid_input.with_extra(str(error)) from error


@router.get("/library/places", summary="Drives the library could go to, and libraries on them")
def library_places() -> Dict[str, Any]:
    return ctx.library_move.places()


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


@router.post("/library/use", summary="Run from a library that is already in another folder")
def use_library(body: LibraryUseRequest = Body()) -> Dict[str, Any]:
    """From the next start, use the library already in ``folder``.

    For a library on a drive that outlived the computer it was made on. Nothing
    is copied, and the library in use now is left where it is.
    """
    try:
        return ctx.library_move.use_existing(body.folder.strip())
    except (OSError, ValueError) as error:
        raise ServerErrors.invalid_input.with_extra(
            f"Cannot use the library in {body.folder}: {error}"
        ) from error


@router.delete("/library/move", summary="Cancel a move; the library stays where it is")
def cancel_library_move() -> Dict[str, Any]:
    return ctx.library_move.cancel()


BACKUP_DESKTOP_LIMIT = 2 * 1024 * 1024 * 1024


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
    final = exports / name
    # With the books on a drive of their own, the folder here holds only
    # covers for those novels: a "backup of the whole library" without the
    # drive plugged in would hold no books and say it had worked.
    drive = ctx.books.folder()
    if drive is not None and not ctx.books.plugged():
        raise ServerError(
            503, f"The books are on {drive}, which is not plugged in: plug it in to back them up"
        )
    # written under another name: one cut short (no room, a pulled cable) used
    # to be left as "the latest backup"
    out = final.with_name(f"{name}.part")

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
        written = set()
        folders = [ctx.files.resolve("novels")]
        if drive is not None:
            folders.append(drive / "novels")
        for novels_dir in folders:
            if not novels_dir.is_dir():
                continue
            for f in novels_dir.rglob("*"):
                arcname = f"novels/{f.relative_to(novels_dir).as_posix()}"
                if arcname not in written and f.is_file():
                    written.add(arcname)
                    zf.write(f, arcname=arcname)

    os.replace(out, final)
    size = final.stat().st_size
    # a copy on the Desktop only when it is small: the Desktop is on the system
    # drive (often synced), the one a big library was moved away from
    saved_to = _copy_to_desktop(final, name) if size < BACKUP_DESKTOP_LIMIT else None
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
