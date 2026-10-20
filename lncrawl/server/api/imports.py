import logging
import os
import shutil
from typing import List, Tuple
from uuid import uuid4

from fastapi import APIRouter, File, Security, UploadFile

from ...context import ctx
from ...dao import Job, User
from ...exceptions import ServerErrors
from ...utils.file_tools import safe_filename
from ..security import ensure_user

logger = logging.getLogger(__name__)

router = APIRouter()

# Uploads are written straight to disk in pieces rather than held in memory: an
# archive of a whole source runs to hundreds of megabytes, and the point of this
# endpoint is to accept exactly that.
CHUNK_SIZE = 1024 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024 * 1024 * 1024


async def _store_upload(upload: UploadFile) -> Tuple[str, str, int]:
    """Spool one upload to disk, returning (stored path, display name, size)."""
    # safe_filename keeps the suffix, which decides how the file is read, and
    # flattens anything path-like in a name the client chose.
    name = safe_filename(os.path.basename(upload.filename or "archive"))
    stored = f"imports/{uuid4().hex}/{name}"
    path = ctx.files.resolve(stored)
    path.parent.mkdir(parents=True, exist_ok=True)

    size = 0
    try:
        with path.open("wb") as out:
            while True:
                chunk = await upload.read(CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_ARCHIVE_BYTES:
                    raise ServerErrors.archive_too_large.with_extra(name)
                out.write(chunk)
    except BaseException:
        shutil.rmtree(path.parent, ignore_errors=True)
        raise
    finally:
        await upload.close()

    return stored, name, size


@router.post(
    "",
    summary="Import novels from uploaded archives",
    description=(
        "Accepts export zips, single ebooks, or json artifacts and queues one "
        "import job per file. Intended for machines with no network access, "
        "where an archive carried in by hand is the only way to stock a library."
    ),
)
async def import_archives(
    files: List[UploadFile] = File(description="Archives or ebooks to import"),
    user: User = Security(ensure_user),
) -> List[Job]:
    uploads = [f for f in files if f and (f.filename or "").strip()]
    if not uploads:
        raise ServerErrors.no_archive_uploaded

    jobs: List[Job] = []
    stored_paths: List[str] = []
    try:
        for upload in uploads:
            stored, name, size = await _store_upload(upload)
            stored_paths.append(stored)
            jobs.append(
                ctx.jobs.import_archive(
                    user,
                    archive_file=stored,
                    archive_name=name,
                    archive_size=size,
                )
            )
    except BaseException:
        # A job that was already queued owns its file and will clean it up, so
        # only the ones with no job behind them are removed here.
        for stored in stored_paths[len(jobs) :]:
            shutil.rmtree(ctx.files.resolve(stored).parent, ignore_errors=True)
        raise

    return jobs
