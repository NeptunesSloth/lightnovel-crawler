import logging
from pathlib import Path
import time

from ....context import ctx
from ....enums import JobType
from ....services.importer import ImportStats, import_archive
from ._base import AbortedException, BaseHandler, HandlerException

logger = logging.getLogger(__name__)

# How often the running totals are written back to the job. An archive can hold
# hundreds of novels, and a database write per novel would cost more than the
# import itself, so progress is reported on a clock rather than per item.
PROGRESS_INTERVAL = 2.0


class ImportArchiveHandler(BaseHandler):
    """Read novels back in from an uploaded archive.

    The counterpart to the export job: where that one bundles a source's novels
    into a zip, this takes such a zip (or a single ebook) and puts the novels
    back into the library — the only way to stock a machine that cannot reach
    the internet. Totals land in ``job.extra`` as they go, so the page can show
    progress the same way it does for an export.
    """

    @staticmethod
    def can_activate(job) -> bool:
        return job.type == JobType.IMPORT_ARCHIVE

    def _discard(self, path, archive_name: str) -> None:
        # The novels are in the library now and each file is kept as its own
        # artifact, so the upload itself is a second copy. Airgapped machines
        # are usually the ones short of disk, so don't leave it behind. A file
        # imported where it already was on this computer is the user's own.
        if not self.job.extra.get("keep_archive"):
            try:
                path.unlink(missing_ok=True)
            except Exception as e:
                logger.warning(f"Could not remove the uploaded archive {archive_name}: {e}")

    def run(self) -> None:
        archive_file = self.job.extra.get("archive_file")
        archive_name = self.job.extra.get("archive_name") or "archive"
        if not archive_file:
            raise HandlerException("No archive was given to import")

        # a file imported where it is on this computer is named by its full path;
        # an upload by its place in the library
        given = Path(archive_file)
        path = given if given.is_absolute() else ctx.files.resolve(archive_file)
        if not path.is_file():
            raise HandlerException(f"The uploaded archive is no longer available: {archive_name}")

        self._set_running()
        self._set_extra(archive_size=path.stat().st_size)

        last_report = 0.0

        def on_progress(member: str, stats: ImportStats) -> None:
            nonlocal last_report
            now = time.monotonic()
            if now - last_report < PROGRESS_INTERVAL:
                return
            last_report = now
            self._set_extra(
                current=member,
                books=stats.books,
                books_read=stats.read,
                imported=stats.novels,
                updated=stats.updated,
                chapters=stats.chapters,
                failed=stats.failed,
            )

        try:
            stats = import_archive(
                path,
                archive_name,
                user_id=self.job.user_id,
                signal=self.signal,
                on_progress=on_progress,
            )
        except AbortedException:
            # Stopped part way (the app closing, a library move): the upload is
            # what the import carries on from at the next start. It used to be
            # deleted here, and the import then failed as "no longer available".
            try:
                if ctx.jobs.get(self.job.id).is_done:  # cancelled for good: nothing to resume
                    self._discard(path, archive_name)
            except Exception:
                pass
            raise
        except BaseException:
            self._discard(path, archive_name)
            raise
        else:
            self._discard(path, archive_name)

        if not stats.novels and not stats.updated:
            reason = ", ".join(f"{n}× {r}" for r, n in stats.reasons.items())
            raise HandlerException(
                f"No novels could be read from {archive_name}"
                + (f" ({reason})" if reason else ". It holds no ebooks this app can read.")
            )

        self._set_extra(
            current="",
            imported=stats.novels,
            updated=stats.updated,
            chapters=stats.chapters,
            failed=stats.failed,
            books=stats.books,
            books_read=stats.read,
            fail_reasons=[{"reason": r, "count": n} for r, n in stats.reasons.items()],
            archive_name=archive_name,
            summary=(
                f"{archive_name}: {stats.novels} new, {stats.updated} updated, "
                f"{stats.chapters} chapters"
            ),
        )
