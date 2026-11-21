"""The books of the library kept on a drive that is not always plugged in."""

from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, Body, Security
import sqlmodel as sq

from ...context import ctx
from ...dao import Novel
from ...exceptions import ServerError
from ...relocation import begin_split, cancel_split, read_marker
from ..security import ensure_admin

router = APIRouter()


def _refuse(error: Exception) -> ServerError:
    return ServerError(400, str(error))


@router.get("", summary="Where the books are: the drive, whether it is plugged in, what is on it")
def book_drive() -> Dict[str, Any]:
    status = ctx.books.status()
    marker = read_marker(ctx.config.app.base_dir)
    status["split_to"] = marker.get("split_to")
    status["split_failed"] = marker.get("split_failed")  # why the last try did not finish
    status["library"] = str(ctx.config.app.app_dir)
    status["base"] = str(ctx.config.app.base_dir)
    return status


@router.post(
    "/split",
    summary="From the next start: keep the library in this folder, its books where they are",
    dependencies=[Security(ensure_admin)],
)
def split_library(folder: str = Body(embed=True)) -> Dict[str, Any]:
    if ctx.books.configured:
        raise ServerError(400, "The books are on a drive of their own already")
    if Path(ctx.config.app.app_dir) == Path(ctx.config.app.base_dir):
        raise ServerError(
            400, "The library is on this computer already: choose a folder on a drive for the books"
        )
    try:
        begin_split(ctx.config.app.base_dir, Path(ctx.config.app.app_dir), Path(folder.strip()))
    except (OSError, ValueError) as error:
        raise _refuse(error) from error
    return book_drive()


@router.post(
    "/drive",
    summary="Keep books in this folder on a drive from now on (then move novels there)",
    dependencies=[Security(ensure_admin)],
)
def make_book_drive(folder: str = Body(embed=True)) -> Dict[str, Any]:
    try:
        ctx.books.create(Path(folder.strip()))
    except (OSError, ValueError) as error:
        raise _refuse(error) from error
    return book_drive()


@router.delete(
    "/split",
    summary="Do not split the library at the next start",
    dependencies=[Security(ensure_admin)],
)
def cancel_split_library() -> Dict[str, Any]:
    cancel_split(ctx.config.app.base_dir)
    return book_drive()


@router.post(
    "/move",
    summary="Move novels' books to the drive, or to this computer",
    dependencies=[Security(ensure_admin)],
)
def move_books(
    ids: List[str] = Body(default=[]),
    to: str = Body(),
    all: bool = Body(default=False),
    keep_copy: bool = Body(
        default=True, description="Brought here, the drive keeps its copy as well"
    ),
) -> Dict[str, Any]:
    if to not in ("drive", "here"):
        raise ServerError(400, 'Books go to "drive" or "here"')
    if all:
        with ctx.db.session() as sess:
            ids = list(sess.exec(sq.select(Novel.id)).all())
    try:
        return ctx.books.move(ids, to == "drive", keep_copy=keep_copy)
    except (OSError, ValueError) as error:
        raise _refuse(error) from error


@router.post(
    "/new",
    summary="Where new novels' books go: the drive (when plugged in) or this computer",
    dependencies=[Security(ensure_admin)],
)
def new_books_go(to: str = Body(embed=True)) -> Dict[str, Any]:
    try:
        ctx.books.set_new(to)
    except ValueError as error:
        raise _refuse(error) from error
    return book_drive()
