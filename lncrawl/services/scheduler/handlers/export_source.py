from concurrent.futures import FIRST_COMPLETED, wait as _wait_futures
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
from time import monotonic
import traceback
from typing import Callable, Dict, List, Optional, Set, Tuple, TypedDict
from urllib.parse import quote, unquote
import zipfile

from bs4 import BeautifulSoup

from ....context import ctx
from ....enums import JobType, OutputFormat
from ....utils.file_tools import atomic_write, safe_filename
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
# is on. The crawler downloads sequentially but with no rate limit, so for a big
# manga it fires requests back-to-back as fast as they complete (many per second),
# which Cloudflare-protected sites flag (it shows up as "ch 0/N"). Capping to one
# request/second keeps the run under the radar. Override per-export via
# extra["requests_per_sec"].
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

# Auto-tuning of the request rate (requests/second). When blocking is detected the
# rate is halved; after a streak of clean novels it creeps back up. Bounds keep it
# both effective and from thrashing.
# How many chapters/images may be in flight at once, whether or not a rate was
# asked for. The rate is held by the task queue's limiter, which hands out send
# slots from one shared clock, so these workers overlap each other's latency
# without ever sending faster than the cap — a worker with no slot yet waits.
#
# This is why the rate is not given to the scraper's own pacer as well: two
# throttles in series is the slower of the two, and the pacer's per-request wait
# cannot be made exact across threads (it reads the last send time, then sleeps,
# so threads wake together — about 2x over the cap when measured). The pacer
# still widens by itself when a site starts refusing, underneath all this.
EXPORT_WORKERS = 4
RPS_MIN = 0.2  # slowest auto-tuned rate (1 request / 5s)
RPS_MAX = 5.0  # fastest auto-tuned rate
RPS_RAISE_AFTER = 3  # consecutive clean novels before nudging the rate up

# Per-novel download watchdog. A single hung request through the single worker can
# otherwise stall a novel — and the whole run — indefinitely. If nothing completes
# within STALL_TIMEOUT, the worker is assumed stuck (the pool is refreshed); if too
# many chapter requests fail back-to-back, the source is blocking and the novel is
# abandoned (it stays partial and is retried/resumed later).
STALL_TIMEOUT = 120.0  # seconds with no completion before giving up on a novel
MAX_CONSECUTIVE_FAILS = 10  # back-to-back request failures before abandoning a novel


class ExportStalledError(HandlerException):
    """Stop an export whose requests stopped completing, retaining checkpoints."""


def _save_stall_diagnostics(job_id: str) -> str:
    """Capture stack locations without locals, response bodies or credentials."""
    relative = f"logs/export-stall-{safe_filename(job_id)}.log"
    lines = ["Export stalled: download worker stacks (no local variable values).\n"]
    frames = sys._current_frames()
    for worker in threading.enumerate():
        if not worker.name.startswith(("lncrawl_scraper", "nodriver-loop")):
            continue
        frame = frames.get(worker.ident) if worker.ident is not None else None
        if frame is not None:
            lines.append(f"\nThread: {worker.name}\n")
            lines.extend(traceback.format_list(traceback.extract_stack(frame)))
    ctx.files.save_text(relative, "".join(lines), compress=False)
    return relative


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


_crash_logged = False


def _log_crash(novel_url: str, error: BaseException) -> None:
    """Write the first download crash's full traceback to a file for diagnosis.

    At the GUI log level nothing is written to a console or log file, so a crash
    like the WinError 6 handle bug leaves no stack trace to work from. Persist the
    first one to ``<data dir>/export-crash.log`` so its exact origin is knowable.
    """
    global _crash_logged
    if _crash_logged:
        return
    _crash_logged = True
    try:
        path = ctx.config.app.app_dir / "export-crash.log"
        text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        path.write_text(f"Export crash for {novel_url}\n\n{text}\n", encoding="utf-8")
        ctx.logger.warn(f"Export crash traceback written to {path}")
    except Exception:
        pass


def _norm_title(title: str) -> str:
    """Normalize a title for cross-source de-duplication."""
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def _looks_throttled(reason: str) -> bool:
    """Whether a failure reason looks like rate-limiting/blocking, not a parse bug."""
    low = (reason or "").lower()
    return any(hint in low for hint in _THROTTLE_HINTS)


def _classify_error(reason: str) -> str:
    """Map a raw failure reason to a short, human label for the live UI."""
    low = (reason or "").lower()
    if any(h in low for h in ("403", "429", "too many", "rate", "forbidden", "blocked")):
        return "blocked"
    if "cloudflare" in low or "captcha" in low or "challenge" in low:
        return "anti-bot"
    if "timeout" in low or "timed out" in low:
        return "timeout"
    if "connection" in low or "ssl" in low or "proxy" in low:
        return "connection"
    if "missing" in low:
        return "incomplete"
    if "no chapters" in low or "could not be built" in low or "no content" in low:
        return "no content"
    return "error"


def _apply_rate(crawler, rps: float) -> None:
    """Set the rate this source is downloaded at. 0 means no cap.

    Goes to the queue's limiter, which admits one call per interval from a clock
    shared by every worker — so the pool stays several deep while the cap holds.
    """
    crawler.taskman.init_executor(
        workers=EXPORT_WORKERS,
        ratelimit=rps if rps and rps > 0 else None,
    )


def _select_novels(urls: List[str], limit: Optional[int] = None) -> List[str]:
    """The distinct novels to export, in discovery order, capped at ``limit``.

    De-duplication has to happen before the cap rather than after it. A source
    lists the same novel from more than one place — its search results and its
    sitemap both turn it up — so trimming first and de-duplicating second hands
    back fewer novels than were asked for, which is the one thing the limit is
    there to control. Asking for 100 is meant to yield 100 novels, not 100
    entries that collapse into 80.
    """
    unique = list(dict.fromkeys(url for url in urls if url))
    if limit and limit > 0:
        return unique[:limit]
    return unique


def _drain(crawler, futures, on_tick, signal) -> int:
    """Resolve download futures with a stall watchdog.

    Returns how many futures were processed. Guards against the two ways a bulk
    run gets stuck on one novel: a hung request (nothing completes within
    ``STALL_TIMEOUT`` → the export stops with its checkpoints retained) and a source
    refusing request after request (``MAX_CONSECUTIVE_FAILS`` back-to-back
    failures → the rest is abandoned). Remaining work is cancelled; the chapters
    that did download are kept (the novel becomes partial and is retried/resumed).
    """
    pending = set(futures)
    done_count = 0
    consecutive_fails = 0
    deadline = monotonic() + STALL_TIMEOUT
    while pending:
        if signal.is_set():
            for f in pending:
                f.cancel()
            raise AbortedException()
        done, pending = _wait_futures(
            pending,
            timeout=min(1.0, max(0.001, deadline - monotonic())),
            return_when=FIRST_COMPLETED,
        )
        if not done:
            if monotonic() < deadline:
                continue
            for f in pending:
                f.cancel()
            signal.set()
            raise ExportStalledError(
                f"No download completed for {STALL_TIMEOUT:g} seconds. Export stopped; "
                "downloaded chapters and completed books are retained. Resume to retry the gaps."
            )
        deadline = monotonic() + STALL_TIMEOUT
        for f in done:
            done_count += 1
            try:
                f.result()
                consecutive_fails = 0
            except Exception:
                consecutive_fails += 1
        if on_tick:
            on_tick(done_count)
        if consecutive_fails >= MAX_CONSECUTIVE_FAILS:
            for f in pending:
                f.cancel()
            break
    return done_count


def _build_index_html(domain: str, entries: List[Tuple[str, str, str]]) -> str:
    """A self-contained clickable index of the novels bundled in the export zip.

    ``entries`` is a list of ``(arcname, display_name, status)``. Opening this file
    from the unzipped folder lists every novel with a link to its ebook so the
    archive can be browsed offline.
    """
    rows = []
    for arcname, name, status in sorted(entries, key=lambda e: e[1].lower()):
        badge = "" if status == "complete" else ' <span class="inc">(incomplete)</span>'
        rows.append(f'<li><a href="{quote(arcname)}">{escape(name)}</a>{badge}</li>')
    total = len(entries)
    return (
        "<!DOCTYPE html><html lang=\"en\"><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape(domain)} — offline library</title><style>"
        "body{margin:0;padding:32px;background:#0f1115;color:#e6e6e6;"
        "font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;line-height:1.6}"
        ".wrap{max-width:760px;margin:0 auto}h1{font-size:22px;margin:0 0 4px}"
        ".sub{color:#8b93a1;margin:0 0 20px;font-size:14px}"
        "ul{list-style:none;padding:0;margin:0}"
        "li{padding:8px 10px;border-bottom:1px solid #20242d}"
        "a{color:#7aa2ff;text-decoration:none}a:hover{text-decoration:underline}"
        ".inc{color:#d98b3a;font-size:12px}</style></head><body><div class='wrap'>"
        f"<h1>{escape(domain)}</h1>"
        f"<p class='sub'>{total} novel(s) in this archive. Click a title to open it.</p>"
        f"<ul>{''.join(rows)}</ul></div></body></html>"
    )


def _manifest_path(domain: str) -> Path:
    """Where the per-source resume manifest of completed novels is stored."""
    return ctx.files.resolve(f"exports/manifest/{safe_filename(domain)}.json")


def _load_manifest_file(domain: str) -> dict:
    path = _manifest_path(domain)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Checkpoint must be a JSON object")
        return data
    except (OSError, ValueError) as error:
        raise HandlerException(f"Could not read export checkpoint {path}: {error}") from error


def _load_manifest(domain: str) -> Dict[str, dict]:
    """Load the resume manifest: ``{novel_url: {"file": str, "missing": int}}``."""
    novels = _load_manifest_file(domain).get("novels")
    return novels if isinstance(novels, dict) else {}


def _load_tuned_rps(domain: str) -> float:
    """The request rate (req/s) the auto-tuner settled on in the last run, or 0."""
    try:
        return float(_load_manifest_file(domain).get("tuned_rps") or 0)
    except Exception:
        return 0.0


def _save_manifest(domain: str, novels: Dict[str, dict], tuned_rps: float = 0) -> None:
    """Persist checkpoints atomically; never silently lose resumability."""
    path = _manifest_path(domain)
    try:
        data = _load_manifest_file(domain)
        data["novels"] = novels
        if tuned_rps > 0:
            data["tuned_rps"] = round(tuned_rps, 2)
        path.parent.mkdir(parents=True, exist_ok=True)
        with atomic_write(path) as temporary:
            temporary.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    except Exception as e:
        raise HandlerException(f"Could not save export checkpoint for {domain}: {e}") from e


def _restore_archived_books(domain: str, manifest: Dict[str, dict]) -> None:
    """Recover completed ebook files from retained export ZIPs without crawling."""
    wanted: Dict[str, List[str]] = {}
    for url, record in manifest.items():
        file = record.get("file")
        if not file or record.get("missing") or Path(file).is_file():
            continue
        name = record.get("archive_name") or Path(file).name
        wanted.setdefault(name, []).append(url)
    if not wanted:
        return
    exports = ctx.files.resolve("exports")
    for archive in sorted(
        exports.glob("*.zip"), key=lambda file: file.stat().st_mtime, reverse=True
    ):
        try:
            with zipfile.ZipFile(archive) as book:
                members = set(book.namelist())
                if "index.html" not in members:
                    continue
                index = BeautifulSoup(book.read("index.html"), "html.parser")
                heading = index.find("h1")
                if not heading or heading.get_text(strip=True) != domain:
                    continue
                for row in index.select("li"):
                    link = row.find("a", href=True)
                    if not link or row.select_one(".inc"):
                        continue
                    name = unquote(str(link["href"]))
                    urls = wanted.get(name, [])
                    if len(urls) != 1 or name not in members:
                        continue
                    url = urls[0]
                    destination = (
                        exports
                        / "reused"
                        / hashlib.sha256(url.encode()).hexdigest()
                        / Path(name).name
                    )
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with atomic_write(destination) as temporary:
                        temporary.write(book.read(name))
                    manifest[url].update(
                        file=str(destination), archive=str(archive), archive_name=name
                    )
                    wanted.pop(name)
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
            ctx.logger.debug(f"Could not recover books from {archive}: {error}")
        if not wanted:
            break


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

    def _set_failure(self, error: str = "", err_source: Optional[Exception] = None) -> bool:
        # Expected watchdog stops get a short message; worker stacks are saved
        # separately for diagnosis without filling the Activity view.
        if isinstance(err_source, ExportStalledError):
            err_source = None
        return super()._set_failure(error, err_source)

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
        # Sitemap mining is what lifts discovery past the one page of results a
        # source's search returns; only turned off when a site's sitemap is junk.
        use_sitemap = bool(self.job.extra.get("use_sitemap", True))
        # How fast to fire chapter/image requests. Polite mode defaults to a gentle
        # rate; a value can be set explicitly to go slower (flagged IP) or faster.
        explicit_rps = float(self.job.extra.get("requests_per_sec", 0) or 0)
        requests_per_sec = explicit_rps
        if polite and requests_per_sec <= 0:
            requests_per_sec = POLITE_REQUESTS_PER_SEC
        auto_tune = bool(self.job.extra.get("auto_tune", True))
        update_only = bool(self.job.extra.get("update_only", False))
        # Speed memory: unless a rate was set explicitly, start at the rate the
        # auto-tuner settled on last run, so a known-touchy site isn't re-blocked
        # while the tuner re-learns from scratch.
        if auto_tune and not explicit_rps:
            saved_rps = _load_tuned_rps(self.job.extra.get("domain") or "")
            if saved_rps > 0:
                requests_per_sec = saved_rps
                ctx.logger.info(f"Starting at remembered rate: {saved_rps} req/s")

        # Seed the de-dupe set with titles already in the library from *other*
        # sources, so a story grabbed from one site isn't redone from another.
        seen_titles: Set[str] = set()
        if dedupe:
            seen_titles = {_norm_title(t) for t in ctx.novels.list_titles(exclude_domain=domain)}

        self._set_running()

        # One crawler session reused for discovery and every download. The scraper
        # is left unpaced on purpose — `_apply_rate` puts this run's cap on the
        # queue instead, where it can be held exactly with several requests in
        # flight. Pacing is the anti-bot measure too, the fix for "ch 0/N" on
        # Cloudflare-protected sources where unthrottled timing gets the IP
        # blocked, so an uncapped run is still the user asking for that risk.
        crawler = ctx.sources.init_crawler(source.url, workers=EXPORT_WORKERS)
        crawler.scraper.signal = self.signal
        crawler.taskman.signal = self.signal
        _apply_rate(crawler, requests_per_sec)
        if requests_per_sec > 0:
            ctx.logger.info(
                f"Export capped at {requests_per_sec} req/s on {domain} "
                f"({EXPORT_WORKERS} in flight)"
            )
        else:
            ctx.logger.info(f"Export uncapped on {domain} ({EXPORT_WORKERS} in flight)")
        try:
            selected = self.job.extra.get("urls")
            if selected:
                # specific novels were picked — export exactly those, so the
                # limit (a cap on how much of a catalogue to take) does not
                # apply and must not silently drop any of them. This is also
                # the resume path: the list persisted below was already capped.
                urls = _select_novels(list(selected))
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
                    use_sitemap=use_sitemap,
                )
                # Discovery stopping on its time cap is why an export comes back
                # with only part of a source; record it so the UI can say so
                # instead of leaving a short catalogue unexplained.
                discovery_capped = monotonic() - discover_start >= discovery_timeout
                self._set_extra(
                    phase="downloading",
                    discovered=len(urls),
                    discovery_capped=discovery_capped,
                )
                if discovery_capped and urls:
                    ctx.logger.warn(
                        f"Discovery on {domain} stopped at its "
                        f"{round(discovery_timeout / 60)} minute cap with {len(urls)} novel(s); "
                        "raise 'Max discovery (min)' to enumerate more of the catalogue"
                    )
                # If discovery hit its time cap and turned up nothing, the source
                # is almost certainly throttling/blocking — fail fast and clearly.
                if not urls and discovery_capped:
                    minutes = round(discovery_timeout / 60)
                    raise HandlerException(
                        f"Discovery found no novels on '{domain}' within {minutes} minutes — "
                        "the source is likely throttling or blocking automated requests. "
                        "Try a smaller limit, enable polite mode, or use a different source."
                    )
                urls = _select_novels(urls, limit)
            if not urls:
                self._set_extra(exported=0, failed=0, total_novels=0)
                return

            # Replay copies job extras: persist discovery once so Resume does
            # not enumerate a different catalogue or reset its selection.
            self._set_extra(urls=urls)

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
            if resume:
                _restore_archived_books(domain, manifest)
                _save_manifest(domain, manifest)
            pending = []
            finished_urls = []
            for novel_url in urls:
                rec = manifest.get(novel_url) if resume else None
                done_file = rec.get("file") if isinstance(rec, dict) else None
                if (
                    done_file
                    and not (rec or {}).get("missing")
                    and Path(done_file).suffix == f".{fmt.value}"
                    and Path(done_file).is_file()
                ):
                    complete[novel_url] = Path(done_file)
                    finished_urls.append(novel_url)
                else:
                    pending.append(novel_url)
                    if done_file and Path(done_file).is_file() and (rec or {}).get("missing"):
                        partial[novel_url] = Path(done_file)

            # Update-only: re-check each finished novel's source page for newly
            # released chapters; those with something new go back to pending (only
            # the new chapters download — the rest are already on disk).
            if update_only and finished_urls:
                self._set_extra(phase="checking-updates")
                for idx, novel_url in enumerate(finished_urls):
                    if self.signal.is_set():
                        raise AbortedException()
                    self._set_progress(idx, len(finished_urls))
                    try:
                        novel = ctx.crawler.fetch_novel(
                            self.user.id, novel_url, signal=self.signal, custom=crawler
                        )
                        all_ids = ctx.chapters.list_ids(novel_id=novel.id)
                        done_ids = set(ctx.chapters.list_ids(novel_id=novel.id, is_crawled=True))
                        if any(cid not in done_ids for cid in all_ids):
                            complete.pop(novel_url, None)
                            pending.append(novel_url)
                            ctx.logger.info(f"New chapters found: {novel.title}")
                    except AbortedException:
                        raise
                    except Exception as e:
                        ctx.logger.debug(f"Update check failed for {novel_url}: {e}")
                self._set_extra(phase="downloading", updated=len(pending))
            if complete:
                ctx.logger.info(
                    f"Resuming {domain}: {len(complete)} novel(s) already done, "
                    f"{len(pending)} to go"
                )
                # surface how many were reused so the UI can show resume working
                self._set_extra(resumed=len(complete), total_novels=total)
                self._set_progress(len(complete), total)

            # Adaptive pacing between novels (see ADAPTIVE_* constants).
            floor_delay = POLITE_DELAY if polite else 0.0
            adaptive_delay = floor_delay

            # Auto-tuned request rate (req/s). Starts at the configured rate (0 =
            # unthrottled, multi-worker) and is adjusted between novels: halved when
            # blocking is seen, nudged up after a clean streak.
            tuned_rps = requests_per_sec
            applied_rps = requests_per_sec
            success_streak = 0

            # Cloudflare clearance is solved in a browser at most once per run, the
            # first time a novel is blocked, then reused for every HTTP request.
            warmed_up = False

            # Per-novel missing-chapter count from the previous pass. Used to stop
            # retrying a novel that isn't improving: if a whole pass fills in no new
            # chapters, the rest are unreachable from this source (deleted/blocked
            # pages) and further rounds just waste time and block the batch. A
            # throttled novel that recovers after the backoff shows fewer missing and
            # keeps going; one whose count holds steady is abandoned as partial.
            prev_missing: Dict[str, int] = {}

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

                    # remember the title for live failure reporting (set once known)
                    cur_title: List[str] = [""]
                    cached_chapters: List[Optional[int]] = [None]

                    # live status so a long run isn't a misleading 0%
                    def _on_novel(title: str, c_done: int, c_total: int) -> None:
                        if cached_chapters[0] is None:
                            cached_chapters[0] = c_done
                        cur_title[0] = title
                        self._set_extra(
                            phase="downloading",
                            current_title=title,
                            current_chapters=c_done,
                            current_total_chapters=c_total,
                            current_reused_chapters=cached_chapters[0],
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
                    except ExportStalledError:
                        try:
                            diagnostic = _save_stall_diagnostics(self.job.id)
                            self._set_extra(
                                stall_diagnostics=diagnostic, stalled_title=cur_title[0]
                            )
                        except Exception as diagnostic_error:
                            ctx.logger.debug(
                                f"Could not save stall diagnostics: {diagnostic_error}"
                            )
                        raise
                    except AbortedException:
                        raise
                    except Exception as e:
                        # Append where it was raised (file:line) so the live UI
                        # pinpoints the source instead of just the message — and
                        # stash the full traceback for the crash log below.
                        tb = e.__traceback__
                        frames = traceback.extract_tb(tb)
                        where = ""
                        if frames:
                            last = frames[-1]
                            fname = os.path.basename(last.filename)
                            where = f" @ {fname}:{last.lineno}"
                        last_error[novel_url] = f"{type(e).__name__}: {e}{where}"[:200]
                        _log_crash(novel_url, e)
                        ctx.logger.debug(f"Export attempt failed for {novel_url}: {e}")
                        result = {"file": None, "missing": 1, "duplicate": False}

                    file = result["file"]
                    if file is not None:
                        manifest[novel_url] = {
                            "file": str(file),
                            "missing": result["missing"],
                            "format": fmt.value,
                        }
                        _save_manifest(domain, manifest)
                    if result["duplicate"]:
                        skipped.add(novel_url)  # already have it — don't retry
                        last_error.pop(novel_url, None)
                    elif file is not None and result["missing"] == 0:
                        complete[novel_url] = file
                        partial.pop(novel_url, None)
                        last_error.pop(novel_url, None)
                        adaptive_delay = max(floor_delay, adaptive_delay * ADAPTIVE_DECAY)
                        # a clean novel: let auto-tune creep the rate back up
                        if auto_tune and tuned_rps > 0:
                            success_streak += 1
                            if success_streak >= RPS_RAISE_AFTER:
                                success_streak = 0
                                tuned_rps = min(RPS_MAX, tuned_rps + 0.5)
                    else:
                        give_up = False
                        if file is not None:
                            partial[novel_url] = file  # keep the best version so far
                            # a partial novel has no exception reason; always refresh
                            # it with the latest missing count so retries that fill in
                            # chapters report the current number, not a stale one
                            last_error[novel_url] = f"{result['missing']} chapter(s) still missing"
                            # stop retrying once a pass makes no progress (the missing
                            # chapters are unreachable, not just throttled) — keep the
                            # partial and move on instead of blocking the batch
                            prev = prev_missing.get(novel_url)
                            prev_missing[novel_url] = result["missing"]
                            if prev is not None and result["missing"] >= prev:
                                give_up = True
                                ctx.logger.info(
                                    f"No progress on {novel_url} "
                                    f"({result['missing']} still missing) — keeping partial"
                                )
                        elif novel_url not in last_error:
                            last_error[novel_url] = "No chapters found or ebook could not be built"
                        if not give_up:
                            next_pending.append(novel_url)
                        reason_text = last_error[novel_url]
                        throttled = _looks_throttled(reason_text)
                        # a failure: slow down (harder if it looks like throttling)
                        bump = ADAPTIVE_STEP * (2 if throttled else 1)
                        adaptive_delay = min(ADAPTIVE_MAX, max(adaptive_delay, floor_delay) + bump)
                        # auto-tune the request rate down when blocking is detected
                        if auto_tune and throttled:
                            success_streak = 0
                            tuned_rps = 1.0 if tuned_rps <= 0 else max(RPS_MIN, tuned_rps / 2)
                        # surface the reason live so the UI shows *why*, not just "ch 0"
                        reason = _classify_error(reason_text)
                        self._set_extra(
                            last_fail_title=cur_title[0] or novel_url,
                            last_fail_reason=reason,
                            last_fail_detail=reason_text[:140],
                        )
                        # First time we hit anti-bot blocking, solve the Cloudflare
                        # challenge once in a browser and reuse the clearance for the
                        # rest of the run (the remaining novels then ride the cleared
                        # HTTP session). Best-effort; no-op without a browser.
                        warm = getattr(crawler, "warm_up_clearance", None)
                        if not warmed_up and reason in ("blocked", "anti-bot") and callable(warm):
                            warmed_up = True
                            # browser warm-up is best-effort; never let it abort the run
                            try:
                                self._set_extra(phase="solving-challenge")
                                cleared = warm(source.url, self.signal)
                                self._set_extra(phase="downloading", browser_cleared=bool(cleared))
                            except Exception as e:
                                ctx.logger.warn(f"Cloudflare warm-up errored: {e}")
                    self._set_progress(len(complete) + len(partial) + len(skipped), total)

                    # apply any auto-tuned rate change before the next novel (safe
                    # here — no download futures are in flight between novels)
                    if auto_tune and tuned_rps != applied_rps:
                        _apply_rate(crawler, tuned_rps)
                        applied_rps = tuned_rps
                        self._set_extra(requests_per_sec=round(tuned_rps, 2))

                    # pace the next novel: adaptive delay climbs on failures and
                    # decays on success, so a run auto-throttles when a site pushes
                    # back. With polite mode off and no failures this is 0 (no wait).
                    if next_pending and adaptive_delay > 0:
                        self._set_extra(
                            phase="waiting-between-novels", adaptive_delay=round(adaptive_delay, 1)
                        )
                        if self.signal.wait(adaptive_delay):
                            raise AbortedException()
                pending = next_pending

            # speed memory: remember the rate the tuner settled on for next run
            if auto_tune and tuned_rps > 0:
                _save_manifest(domain, manifest, tuned_rps=tuned_rps)

            # bundle the complete ones, plus the best partial we have for the rest.
            # Iterate `partial` directly (not just novels still in `pending`) so a
            # novel we stopped retrying early — because it stopped improving — is
            # still included as an incomplete book rather than silently dropped.
            files: List[Path] = list(complete.values())
            for novel_url, partial_file in partial.items():
                if novel_url not in complete:
                    files.append(partial_file)
            # a novel is "failed" only if it produced no book at all (no partial)
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

            partial_paths = set(partial.values())
            export_rel = f"exports/{self.job.id}.zip"
            export_path = ctx.files.resolve(export_rel)
            export_path.parent.mkdir(parents=True, exist_ok=True)
            index_entries: List[Tuple[str, str, str]] = []
            zipped = 0
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
                    # never let one unreadable/vanished file abort the whole zip —
                    # that would throw away the entire run at the very last step
                    try:
                        zf.write(file, arcname=arcname)
                    except Exception as e:
                        ctx.logger.warn(f"Skipping {file} in export zip: {e}")
                        continue
                    zipped += 1
                    for record in manifest.values():
                        if record.get("file") == str(file):
                            record.update(archive=str(export_path), archive_name=arcname)
                    status = "incomplete" if file in partial_paths else "complete"
                    index_entries.append((arcname, file.stem, status))
                # a clickable offline index of everything in the archive
                zf.writestr("index.html", _build_index_html(domain, index_entries))
            _save_manifest(domain, manifest)
            if not zipped:
                raise HandlerException("Could not add any novel to the export zip")

            # report counts from what actually made it into the zip, so a file
            # that couldn't be written isn't silently counted as complete
            zipped_complete = sum(1 for _, _, s in index_entries if s == "complete")
            zipped_incomplete = len(index_entries) - zipped_complete
            zip_skipped = len(files) - zipped

            export_name = f"{domain}-novels.zip"
            saved_to = _copy_to_desktop(export_path, export_name)

            self._set_extra(
                export_file=export_rel,
                export_name=export_name,
                export_size=export_path.stat().st_size,
                saved_to=saved_to,
                exported=zipped,
                complete=zipped_complete,
                incomplete=zipped_incomplete,
                failed=failed,
                skipped=len(skipped),
                zip_skipped=zip_skipped,
                total_novels=total,
                fail_reasons=fail_reasons,
            )

            size_mb = export_path.stat().st_size / (1024 * 1024)
            summary = (
                f"{domain}: {zipped} of {total} novel(s) exported "
                f"({zipped_complete} complete, {zipped_incomplete} partial, {failed} failed, "
                f"{len(skipped)} skipped) — {size_mb:.1f} MB."
            )
            if zip_skipped:
                summary += f" {zip_skipped} could not be added to the zip."
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
    # — a wasted network round-trip per novel per round. Only fetch when new, or
    # when the stored record predates synopsis/tags parsing (backfills old
    # library entries with a single page request on the next run).
    novel = ctx.novels.find_by_url(novel_url)
    if novel is None or not ctx.chapters.list_ids(novel_id=novel.id) or not novel.synopsis:
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
    pending_chapters = ctx.crawler.pending_chapter_ids(chapter_ids)
    chapter_futures = [
        crawler.taskman.submit_task(ctx.crawler.fetch_chapter, user_id, cid, custom=crawler)
        for cid in pending_chapters
    ]
    c_total = len(chapter_ids)
    c_cached = c_total - len(chapter_futures)
    if on_novel:
        on_novel(novel.title, c_cached, c_total)
    last_emit = [monotonic()]

    def _tick(done: int) -> None:
        # report at least every ~2s (and at the end) so the UI reflects real
        # movement instead of looking frozen on a big, slow novel
        if on_novel and (done + c_cached == c_total or monotonic() - last_emit[0] >= 2.0):
            last_emit[0] = monotonic()
            on_novel(novel.title, done + c_cached, c_total)

    # drain with the stall watchdog so one hung request can't freeze the run
    _drain(crawler, chapter_futures, _tick, signal)

    # download chapter images in one batch (covers manga pages and inline art);
    # one query for the whole novel instead of one per chapter
    image_ids = ctx.images.list_ids(novel_id=novel.id)
    if image_ids:
        image_futures = [
            crawler.taskman.submit_task(ctx.crawler.fetch_image, user_id, iid, custom=crawler)
            for iid in sorted(set(image_ids))
        ]
        _drain(crawler, image_futures, None, signal)

    # how many of the targeted chapters are still not downloaded (drives retries)
    # Old completion flags can outlive blank or missing content files. Inspect
    # the targeted files so those gaps stay eligible for the next retry pass.
    downloaded = ctx.chapters.get_many(chapter_ids)
    missing = len(chapter_ids) - sum(chapter.is_available for chapter in downloaded)

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
