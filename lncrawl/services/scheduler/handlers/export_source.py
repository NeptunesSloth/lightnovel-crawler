from pathlib import Path
from typing import List, Optional
import zipfile

from ....context import ctx
from ....enums import JobType, OutputFormat
from ._base import AbortedException, BaseHandler, HandlerException


class ExportSourceHandler(BaseHandler):
    """Download every novel from a source and bundle the ebooks into one zip.

    This is a long-running single job: it discovers the source's catalogue,
    fully downloads each novel (chapters + images, so manga pages are included),
    builds an ebook for each, and packages them all into a single downloadable
    ``.zip``. The result path is stored in ``job.extra["export_file"]``.
    """

    @staticmethod
    def can_activate(job) -> bool:
        return job.type == JobType.EXPORT_SOURCE

    def run(self) -> None:
        domain = self.job.extra.get("domain")
        if not domain:
            raise HandlerException("Source domain is not specified")

        source = ctx.sources.get_source(domain)
        fmt = OutputFormat(self.job.extra.get("format") or OutputFormat.epub.value)
        limit = self.job.extra.get("limit")

        self._set_running()

        # one crawler session reused for discovery and every download
        crawler = ctx.sources.init_crawler(source.url)
        try:
            urls = ctx.crawler.discover_novels(self.user.id, domain, self.signal, custom=crawler)
            if limit:
                urls = urls[:limit]
            if not urls:
                self._set_extra(exported=0, failed=0, total_novels=0)
                return

            self._set_progress(0, len(urls))

            files: List[Path] = []
            failed = 0
            for index, novel_url in enumerate(urls, start=1):
                if self.signal.is_set():
                    raise AbortedException()
                try:
                    file = _download_novel_file(crawler, self.user.id, novel_url, fmt, self.signal)
                    if file:
                        files.append(file)
                    else:
                        failed += 1
                except AbortedException:
                    raise
                except Exception as e:
                    ctx.logger.debug(f"Export failed for {novel_url}: {e}")
                    failed += 1
                self._set_progress(index, len(urls))

            if not files:
                self._set_extra(exported=0, failed=failed, total_novels=len(urls))
                raise HandlerException("Could not export any novel from the source")

            # bundle everything into a single zip on disk
            export_rel = f"exports/{self.job.id}.zip"
            export_path = ctx.files.resolve(export_rel)
            export_path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(export_path, "w", zipfile.ZIP_DEFLATED) as zf:
                used: set = set()
                for file in files:
                    arcname = file.name
                    # avoid clobbering on duplicate titles
                    counter = 1
                    while arcname in used:
                        arcname = f"{file.stem} ({counter}){file.suffix}"
                        counter += 1
                    used.add(arcname)
                    zf.write(file, arcname=arcname)

            self._set_extra(
                export_file=export_rel,
                export_name=f"{domain}-novels.zip",
                export_size=export_path.stat().st_size,
                exported=len(files),
                failed=failed,
                total_novels=len(urls),
            )
        finally:
            crawler.close()

    def _set_progress(self, done: int, total: int) -> None:
        with ctx.db.session() as sess:
            ctx.jobs._update(sess, self.job.id, done=done, total=max(total, 1))
            sess.commit()
        self.job.done = done
        self.job.total = max(total, 1)
        ctx.job_notifier.notify(self.user, self.job)


def _download_novel_file(
    crawler,
    user_id: str,
    novel_url: str,
    fmt: OutputFormat,
    signal,
) -> Optional[Path]:
    # fetch metadata (syncs chapters/volumes into the DB)
    novel = ctx.crawler.fetch_novel(user_id, novel_url, signal=signal, custom=crawler)
    chapter_ids = ctx.chapters.list_ids(novel_id=novel.id)
    if not chapter_ids:
        return None

    # download all chapters
    chapter_futures = [
        crawler.taskman.submit_task(ctx.crawler.fetch_chapter, user_id, cid, custom=crawler)
        for cid in sorted(set(chapter_ids))
    ]
    image_ids: List[str] = []
    for chapter in crawler.taskman.resolve(chapter_futures, desc="Chapters", unit=" c"):
        if chapter:
            image_ids += ctx.images.list_ids(chapter_id=chapter.id)

    # download chapter images (covers manga pages and inline novel art)
    if image_ids:
        image_futures = [
            crawler.taskman.submit_task(ctx.crawler.fetch_image, user_id, iid, custom=crawler)
            for iid in sorted(set(image_ids))
        ]
        crawler.taskman.resolve_futures(image_futures, desc="Images", unit=" img")

    # build the ebook. Formats derived from epub need an epub built first.
    epub_artifact = None
    if fmt == OutputFormat.epub or fmt in ctx.binder.depends_on_epub:
        epub_artifact = ctx.binder.make_artifact(
            novel.id, novel.title, format=OutputFormat.epub, user_id=user_id
        )
    if fmt == OutputFormat.epub:
        artifact = epub_artifact
    else:
        artifact = ctx.binder.make_artifact(
            novel.id, novel.title, format=fmt, user_id=user_id, epub=epub_artifact
        )

    if not artifact or not artifact.is_available:
        return None
    return ctx.files.resolve(artifact.output_file)
