import json
import os
from pathlib import Path
import re
import shutil
from time import monotonic
from typing import Callable, Dict, List, Optional, Set, TypedDict
import zipfile

from ....context import ctx
from ....enums import JobType, OutputFormat
from ....utils.file_tools import safe_filename
from ....utils.notify import notify_finished
from ._base import AbortedException, BaseHandler, HandlerException


class NovelResult(TypedDict):
    file: Optional[Path]  # the built ebook, or None if it couldn't be made
    missing: int  # chapters still not downloaded
    duplicate: bool  # already present (same title) from another source


# How many extra passes to make over novels that failed or came out incomplete,
# unless the job overrides it via extra["retries"]. Re-downloads are cheap since
# already-fetched chapters are skipped, so this is safe to run overnight.
DEFAULT_RETRIES = 3

# Seconds to pause between novels when "polite mode" is on, to avoid tripping a
# site's rate-limiting / anti-bot protection during a big bulk download.
POLITE_DELAY = 3.0

# Default request rate (per second) for chapter/image downloads when polite mode
# is on. Without this, a novel's chapters/pages download 5-at-a-time in a burst,
# which Cloudflare-protected sites block outright (it shows up as "ch 0/N"). One
# request/second through a single worker keeps the run under the radar. Users can
# override it per-export via extra["requests_per_sec"].
POLITE_REQUESTS_PER_SEC = 1.0

# Wait before each retry round (seconds, grows per round, capped). Gives a
# throttled/blocked source time to recover so the retry isn't all failures again.
RETRY_BACKOFF = 30.0
MAX_RETRY_BACKOFF = 300.0

# Adaptive per-novel pacing. The delay between novels climbs when downloads start
# failing (so an overnight run automatically slows down when a site begins
# throttling it) and decays back toward the floor as they succeed again. This is
# active even with polite mode off — the floor is just 0 in that case.
ADAPTIVE_STEP = 8.0  # seconds added per failed novel (doubled when it looks throttled)
ADAPTIVE_MAX = 60.0  # never wait more than this between novels
ADAPTIVE_DECAY = 0.7  # multiply the delay by this after each success

# Substrings in a failure reason that suggest the source is rate-limiting/blocking
# us (as opposed to a parse error), so we should back off harder.
_THROTTLE_HINTS = (
    "429",
    "403",
    "too many",
    "rate limit",
    "ratelimit",
    "timeout",
    "timed out",
    "connection",
    "blocked",
    "captcha",
    "cloudflare",
    "forbidden",
)


def _norm_title(title: str) -> str:
    """Normalize a title for cross-source de-duplication."""
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def _looks_throttled(reason: str) -> bool:
    """Whether a failure reason looks like rate-limiting/blocking, not a parse bug."""
    low = (reason or "").lower()
    return any(hint in low for hint in _THROTTLE_HINTS)


def _manifest_path(domain: str) -> Path:
    """Where the per-source resume manifest of completed novels is stored."""
    return ctx.files.resolve(f"exports/manifest/{safe_filename(domain)}.json")


def _load_manifest(domain: str) -> Dict[str, dict]:
    """Load the resume manifest: ``{novel_url: {"file": str, "missing": int}}``."""
    path = _manifest_path(domain)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        novels = data.get("novels")
        return novels if isinstance(novels, dict) else {}
    except Exception:
        return {}


def _save_manifest(domain: str, novels: Dict[str, dict]) -> None:
    """Persist the resume manifest (best-effort; never raises into the caller)."""
    path = _manifest_path(domain)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"novels": novels}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:
        ctx.logger.debug(f"Could not save export manifest for {domain}: {e}")


def _find_desktop() -> Optional[Path]:
    """Locate the user's real Desktop, accounting for OneDrive redirection."""
    home = Path.home()
    candidates = [
        home / "OneDrive" / "Desktop",  # common Windows OneDrive redirect
        home / "Desktop",
    ]
    one_drive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
    if one_drive:
        candidates.insert(0, Path(one_drive) / "Desktop")
    for path in candidates:
        if path.is_dir():
            return path
    return None


def _copy_to_desktop(src: Path, name: str) -> Optional[str]:
    """Copy the finished export onto the user's Desktop (for the local app).

    Returns the destination path, or None when there's no Desktop folder (e.g. a
    headless server) or the copy fails.
    """
    desktop = _find_desktop()
    if desktop is None:
        return None
    dest = desktop / name
    if dest.exists():
        # don't clobber a previous export of the same source
        dest = desktop / f"{dest.stem}-{src.stem[:8]}{dest.suffix}"
    try:
        shutil.copy2(src, dest)
        ctx.logger.info(f"Export saved to Desktop: {dest}")
        return str(dest)
    except Exception as e:
        ctx.logger.debug(f"Could not copy export to Desktop: {e}")
        return None


class ExportSourceHandler(BaseHandler):
    """Download every novel from a source and bundle the ebooks into one zip.

    This is a long-running single job: it discovers the source's catalogue,
    fully downloads each novel (chapters + images, so manga pages are included),
    builds an ebook for each, and packages them all into a single downloadable
    ``.zip``. Novels that fail or come out missing chapters are retried over
    several passes so an overnight run finishes as complete as possible. The
    result path is stored in ``job.extra["export_file"]``.
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
        retries = int(self.job.extra.get("retries", DEFAULT_RETRIES))
        polite = bool(self.job.extra.get("polite"))
        dedupe = bool(self.job.extra.get("dedupe", True))
        discovery_timeout = float(self.job.extra.get("discovery_minutes", 10)) * 60
        max_chapters = int(self.job.extra.get("max_chapters", 0))
        resume = bool(self.job.extra.get("resume", True))
        # How fast to fire chapter/image requests. Polite mode defaults to a gentle
        # rate; a value can be set explicitly to go slower (flagged IP) or faster.
        requests_per_sec = float(self.job.extra.get("requests_per_sec", 0) or 0)
        if polite and requests_per_sec <= 0:
            requests_per_sec = POLITE_REQUESTS_PER_SEC

        # Seed the de-dupe set with titles already in the library from *other*
        # sources, so a story grabbed from one site isn't redone from another.
        seen_titles: Set[str] = set()
        if dedupe:
            seen_titles = {_norm_title(t) for t in ctx.novels.list_titles(exclude_domain=domain)}

        self._set_running()

        # one crawler session reused for discovery and every download
        crawler = ctx.sources.init_crawler(source.url)
        if requests_per_sec > 0:
            # Pace chapter/image downloads through a single worker so a novel's
            # requests don't burst all at once and trip a site's anti-bot. This is
            # the real fix for "ch 0/N" on Cloudflare-protected sources.
            crawler.taskman.init_executor(ratelimit=requests_per_sec)
            ctx.logger.info(f"Export throttled to {requests_per_sec} req/s on {domain}")
        try:
            selected = self.job.extra.get("urls")
            if selected:
                # specific novels were picked — export exactly those
                urls = list(selected)
            else:
                # Discovery runs before any download; drive the progress bar with
                # seed progress so a slow/throttled source isn't a silent 0%.
                def _on_discover(done: int, seeds: int, found: int) -> None:
                    self._set_extra(phase="discovering", found=found)
                    self._set_progress(done, seeds)

                discover_start = monotonic()
                urls = ctx.crawler.discover_novels(
                    self.user.id,
                    domain,
                    self.signal,
                    custom=crawler,
                    on_progress=_on_discover,
                    time_budget=discovery_timeout,
                    delay=POLITE_DELAY if polite else 0,
                )
                self._set_extra(phase="downloading")
                # If discovery hit its time cap and turned up nothing, the source
                # is almost certainly throttling/blocking — fail fast and clearly.
                if not urls and monotonic() - discover_start >= discovery_timeout:
                    minutes = round(discovery_timeout / 60)
                    raise HandlerException(
                        f"Discovery found no novels on '{domain}' within {minutes} minutes — "
                        "the source is likely throttling or blocking automated requests. "
                        "Try a smaller limit, enable polite mode, or use a different source."
                    )
                if limit:
                    urls = urls[:limit]
            if not urls:
                self._set_extra(exported=0, failed=0, total_novels=0)
                return

            total = len(urls)
            self._set_progress(0, total)

            complete: Dict[str, Path] = {}  # fully downloaded (no missing chapters)
            partial: Dict[str, Path] = {}  # ebook built but some chapters missing
            last_error: Dict[str, str] = {}  # most recent failure reason per novel
            skipped: Set[str] = set()  # duplicates already had from another source

            # Auto-resume across runs: a per-source manifest remembers which novels
            # were fully exported (and the ebook still on disk), so re-running after
            # a crash/stop or just to grab the rest reuses them instead of starting
            # over. Only complete novels are recorded; partial/failed ones are always
            # re-attempted (cheap, since downloaded chapters are skipped).
            manifest = _load_manifest(domain) if resume else {}
            pending = []
            for novel_url in urls:
                rec = manifest.get(novel_url) if resume else None
                done_file = rec.get("file") if isinstance(rec, dict) else None
                if done_file and not (rec or {}).get("missing") and Path(done_file).is_file():
                    complete[novel_url] = Path(done_file)
                else:
                    pending.append(novel_url)
            if complete:
                ctx.logger.info(
                    f"Resuming {domain}: {len(complete)} novel(s) already done, "
                    f"{len(pending)} to go"
                )
                self._set_progress(len(complete), total)

            # Adaptive pacing between novels (see ADAPTIVE_* constants).
            floor_delay = POLITE_DELAY if polite else 0.0
            adaptive_delay = floor_delay

            # initial pass + retry rounds over whatever still isn't complete
            for attempt in range(retries + 1):
                if not pending:
                    break
                if attempt > 0:
                    ctx.logger.info(
                        f"Export retry {attempt}/{retries}: {len(pending)} novel(s) left on {domain}"
                    )
                    # back off before each retry round so a throttled/blocked
                    # source has time to recover instead of failing everything again
                    backoff = min(RETRY_BACKOFF * attempt, MAX_RETRY_BACKOFF)
                    self._set_extra(phase="retry-waiting", retry=attempt)
                    if self.signal.wait(backoff):
                        raise AbortedException()
                    self._set_extra(phase="downloading")
                next_pending: List[str] = []
                for novel_url in pending:
                    if self.signal.is_set():
                        raise AbortedException()

                    # live status so a long run isn't a misleading 0%
                    def _on_novel(title: str, c_done: int, c_total: int) -> None:
                        self._set_extra(
                            phase="downloading",
                            current_title=title,
                            current_chapters=c_done,
                            current_total_chapters=c_total,
                            done_novels=len(complete) + len(partial) + len(skipped),
                            total_novels=total,
                        )

                    result: NovelResult
                    try:
                        result = _download_novel(
                            crawler,
                            self.user.id,
                            novel_url,
                            fmt,
                            self.signal,
                            seen_titles,
                            dedupe,
                            max_chapters,
                            _on_novel,
                        )
                    except AbortedException:
                        raise
                    except Exception as e:
                        last_error[novel_url] = f"{type(e).__name__}: {e}"[:200]
                        ctx.logger.debug(f"Export attempt failed for {novel_url}: {e}")
                        result = {"file": None, "missing": 1, "duplicate": False}

                    file = result["file"]
                    if result["duplicate"]:
                        skipped.add(novel_url)  # already have it — don't retry
                        last_error.pop(novel_url, None)
                    elif file is not None and result["missing"] == 0:
                        complete[novel_url] = file
                        partial.pop(novel_url, None)
                        last_error.pop(novel_url, None)
                        # record in the resume manifest so a re-run skips it
                        manifest[novel_url] = {"file": str(file), "missing": 0}
                        _save_manifest(domain, manifest)
                        adaptive_delay = max(floor_delay, adaptive_delay * ADAPTIVE_DECAY)
                    else:
                        if file is not None:
                            partial[novel_url] = file  # keep the best version so far
                        elif novel_url not in last_error:
                            last_error[novel_url] = "No chapters found or ebook could not be built"
                        next_pending.append(novel_url)
                        # a failure: slow down (harder if it looks like throttling)
                        bump = ADAPTIVE_STEP * (2 if _looks_throttled(last_error[novel_url]) else 1)
                        adaptive_delay = min(ADAPTIVE_MAX, max(adaptive_delay, floor_delay) + bump)
                    self._set_progress(len(complete) + len(partial) + len(skipped), total)

                    # pace the next novel: adaptive delay climbs on failures and
                    # decays on success, so a run auto-throttles when a site pushes
                    # back. With polite mode off and no failures this is 0 (no wait).
                    if next_pending and adaptive_delay > 0:
                        self._set_extra(
                            phase="downloading", adaptive_delay=round(adaptive_delay, 1)
                        )
                        if self.signal.wait(adaptive_delay):
                            raise AbortedException()
                pending = next_pending

            # bundle the complete ones, plus the best partial we have for the rest
            files: List[Path] = list(complete.values())
            for novel_url in pending:
                if novel_url in partial:
                    files.append(partial[novel_url])
            incomplete = sum(1 for u in pending if u in partial)
            failed = sum(1 for u in pending if u not in partial)

            # tally the failure reasons so the UI can show *why* novels failed
            reason_tally: Dict[str, int] = {}
            for u in pending:
                if u not in partial:
                    reason = last_error.get(u, "Unknown error")
                    reason_tally[reason] = reason_tally.get(reason, 0) + 1
            fail_reasons = [
                {"reason": r, "count": c}
                for r, c in sorted(reason_tally.items(), key=lambda x: -x[1])[:5]
            ]

            if not files:
                self._set_extra(
                    exported=0,
                    incomplete=0,
                    failed=failed,
                    skipped=len(skipped),
                    total_novels=total,
                    fail_reasons=fail_reasons,
                )
                # if everything was a known duplicate, that's a success, not an error
                if skipped and not failed:
                    self._notify_finish(
                        f"{domain}: nothing new to export — all {len(skipped)} already in "
                        "your library."
                    )
                    return
                self._notify_finish(
                    f"{domain}: export failed — could not build any of {total} novel(s). "
                    "The source is likely throttling or blocking requests."
                )
                raise HandlerException("Could not export any novel from the source")

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

            export_name = f"{domain}-novels.zip"
            saved_to = _copy_to_desktop(export_path, export_name)

            self._set_extra(
                export_file=export_rel,
                export_name=export_name,
                export_size=export_path.stat().st_size,
                saved_to=saved_to,
                exported=len(files),
                complete=len(complete),
                incomplete=incomplete,
                failed=failed,
                skipped=len(skipped),
                total_novels=total,
                fail_reasons=fail_reasons,
            )

            size_mb = export_path.stat().st_size / (1024 * 1024)
            summary = (
                f"{domain}: {len(files)} of {total} novel(s) exported "
                f"({len(complete)} complete, {incomplete} partial, {failed} failed, "
                f"{len(skipped)} skipped) — {size_mb:.1f} MB."
            )
            if saved_to:
                summary += " Saved to your Desktop."
            self._notify_finish(summary)
        finally:
            crawler.close()

    def _set_progress(self, done: int, total: int) -> None:
        with ctx.db.session() as sess:
            ctx.jobs._update(sess, self.job.id, done=done, total=max(total, 1))
            sess.commit()
        self.job.done = done
        self.job.total = max(total, 1)
        ctx.job_notifier.notify(self.user, self.job)

    def _notify_finish(self, summary: str) -> None:
        """Fire the configured desktop/webhook finish notifications (best-effort)."""
        cfg = ctx.config.notify
        if not cfg.desktop_toast and not cfg.webhook_url:
            return
        try:
            notify_finished(
                title="Lightnovel Crawler — export finished",
                message=summary,
                webhook_url=cfg.webhook_url,
                desktop=cfg.desktop_toast,
            )
        except Exception as e:
            ctx.logger.debug(f"Finish notification failed: {e}")


def _download_novel(
    crawler,
    user_id: str,
    novel_url: str,
    fmt: OutputFormat,
    signal,
    seen_titles: Optional[Set[str]] = None,
    dedupe: bool = False,
    max_chapters: int = 0,
    on_novel: Optional[Callable[[str, int, int], None]] = None,
) -> NovelResult:
    """Download a novel and build its ebook.

    Returns the built file (or None) and how many chapters are still missing.
    Already-downloaded chapters are skipped, so calling this again only fills in
    what's still missing — which is what makes the retry passes cheap. If
    ``dedupe`` is on and the (normalized) title is already in ``seen_titles``,
    the novel is reported as a duplicate without downloading anything.
    ``max_chapters`` (>0) caps how many chapters are pulled per novel so one huge
    series can't stall the whole run. ``on_novel(title, done, total)`` reports
    live chapter progress.
    """
    # Reuse already-fetched metadata: on retry rounds (and re-runs) the novel and
    # its chapter list are already in the DB, so skip re-downloading the info page
    # — a wasted network round-trip per novel per round. Only fetch when new.
    novel = ctx.novels.find_by_url(novel_url)
    if novel is None or not ctx.chapters.list_ids(novel_id=novel.id):
        novel = ctx.crawler.fetch_novel(user_id, novel_url, signal=signal, custom=crawler)

    norm = _norm_title(novel.title)
    if dedupe and seen_titles is not None and norm in seen_titles:
        return {"file": None, "missing": 0, "duplicate": True}

    # first N chapters when capped, so a giant series doesn't eat the whole run
    chapter_ids = ctx.chapters.list_ids(novel_id=novel.id, limit=max_chapters or None)
    if not chapter_ids:
        return {"file": None, "missing": 0, "duplicate": False}

    # download the chapters (fetch_chapter skips ones already fetched), reporting
    # progress as each one completes
    chapter_futures = [
        crawler.taskman.submit_task(ctx.crawler.fetch_chapter, user_id, cid, custom=crawler)
        for cid in sorted(set(chapter_ids))
    ]
    c_total = len(chapter_futures)
    if on_novel:
        on_novel(novel.title, 0, c_total)
    c_done = 0
    last_emit = monotonic()
    for _ in crawler.taskman.resolve(chapter_futures, desc="Chapters", unit=" c"):
        c_done += 1
        # report at least every ~2s (and at the end) so the UI reflects real
        # movement instead of looking frozen on a big, slow novel
        if on_novel and (c_done == c_total or monotonic() - last_emit >= 2.0):
            last_emit = monotonic()
            on_novel(novel.title, c_done, c_total)

    # download chapter images in one batch (covers manga pages and inline art);
    # one query for the whole novel instead of one per chapter
    image_ids = ctx.images.list_ids(novel_id=novel.id)
    if image_ids:
        image_futures = [
            crawler.taskman.submit_task(ctx.crawler.fetch_image, user_id, iid, custom=crawler)
            for iid in sorted(set(image_ids))
        ]
        crawler.taskman.resolve_futures(image_futures, desc="Images", unit=" img")

    # how many of the targeted chapters are still not downloaded (drives retries)
    done_ids = set(ctx.chapters.list_ids(novel_id=novel.id, is_crawled=True))
    missing = len([c for c in chapter_ids if c not in done_ids])

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

    file = None
    if artifact and artifact.is_available:
        file = ctx.files.resolve(artifact.output_file)
        if seen_titles is not None:
            seen_titles.add(norm)  # remember it so other sources skip this story
    return {"file": file, "missing": missing, "duplicate": False}
