import json
import logging
from pathlib import Path
import re
from threading import Event
import zipfile

from ...context import ctx
from ...dao import Artifact
from ...exceptions import AbortedException
from ...utils.file_tools import move_into_place

logger = logging.getLogger(__name__)

# Stored chapter HTML embeds each page image as <img src="images/<id>.jpg">
# (see Crawler._extract_images). The order they appear in the body is the
# reading order, which is exactly what a CBZ needs.
# What the archive says about itself, read back by the importer.
ABOUT = "lncrawl.json"

RE_IMG = re.compile(r"""<img[^>]+src=["']images/([0-9a-fA-F]+)\.jpg["']""")


def make_cbz(working_dir: Path, artifact: Artifact, signal: Event = Event(), **kwargs) -> None:
    """Bundle a novel's page images into a ``.cbz`` comic archive (manga-friendly).

    A CBZ is just a ZIP of images that readers display in alphabetical order, so
    every page is named with zero-padded chapter and page numbers to preserve the
    reading order. Chapters with no images are skipped; if the novel has no images
    at all (e.g. a text-only light novel) nothing is produced, and the export
    treats that novel as "could not build" for this format.
    """
    out_file = ctx.files.resolve(artifact.output_file)
    tmp_file = working_dir / out_file.name

    novel = ctx.novels.get(artifact.novel_id)

    pages: list = []  # (arcname, source Path)
    # Which novel this is, and which page is which, kept in the archive: a CBZ
    # said neither, so importing one made a second novel named after the file
    # ("Danmachi  Sword S Oratoria") beside the one it came from.
    about: dict = {
        "title": novel.title,
        "url": novel.url,
        "authors": novel.authors,
        "language": novel.language,
        "tags": list(novel.tags or []),
        "chapters": {},
        "pages": {},
    }

    # lead with the cover so the archive opens on it
    if novel.cover_available:
        cover = ctx.files.resolve(novel.cover_file)
        if cover.is_file():
            pages.append(("00000_0000_cover.jpg", cover))

    for chapter in ctx.chapters.list(novel_id=artifact.novel_id):
        if signal.is_set():
            raise AbortedException()
        if not chapter.is_available:
            continue
        try:
            body = ctx.files.load_text(chapter.content_file) or ""
        except Exception:
            continue
        page_no = 0
        for image_id in RE_IMG.findall(body):
            file = ctx.files.resolve(f"novels/{artifact.novel_id}/images/{image_id}.jpg")
            if not file.is_file():
                continue
            page_no += 1
            arcname = f"{chapter.serial:05}_{page_no:04}.jpg"
            pages.append((arcname, file))
            about["pages"][arcname] = image_id
        if page_no:
            about["chapters"][str(chapter.serial)] = chapter.title

    if not pages:
        logger.info(f"No page images to bundle into CBZ for novel {artifact.novel_id}")
        return

    # JPEGs are already compressed, so store them without recompressing (faster,
    # and the conventional choice for comic archives).
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(tmp_file, "w", zipfile.ZIP_STORED) as zf:
        for arcname, file in pages:
            zf.write(file, arcname=arcname)
        zf.writestr(ABOUT, json.dumps(about, ensure_ascii=False))
    move_into_place(tmp_file, out_file)
    logger.info(f"Created: {out_file} ({len(pages)} pages)")
