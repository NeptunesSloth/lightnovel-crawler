from pathlib import Path
import sqlite3
import time
from typing import Any, Dict, List, Literal, Union
import zipfile

from fastapi import APIRouter, Body, Query
from fastapi.responses import FileResponse
from sqlalchemy.engine import make_url

from ...context import ctx
from ...exceptions import ServerErrors
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

# The root router
router = APIRouter()


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


@router.post("/update-sources", summary="Update sources from the repository")
async def update() -> int:
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
