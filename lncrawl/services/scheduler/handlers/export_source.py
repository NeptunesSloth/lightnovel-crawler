import errno
import hashlib
from html import escape
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
from time import monotonic, strftime
import traceback
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, TypedDict
from urllib.parse import quote, unquote
import zipfile

from bs4 import BeautifulSoup
from typing_extensions import NotRequired

from ....context import ctx
from ....core.crawler import is_own_host
from ....core.scraping import IMAGE_CONNECTIONS
from ....enums import JobStatus, JobType, OutputFormat
from ....utils.file_tools import atomic_write, safe_filename, writable_folder
from ....utils.keep_awake import keep_awake
from ....utils.notify import notify_finished
from ....utils.time_utils import current_timestamp
from ...crawl_engine import CrawlStalledError, crawl_items
from ._base import AbortedException, BaseHandler, HandlerException
from .export_pace import RPS_MAX, PaceController, estimate_left, pressure


class NovelResult(TypedDict):
    file: Optional[Path]  # the built ebook, or None if it couldn't be made
    missing: int  # chapters still not downloaded
    duplicate: bool  # already present (same title) from another source
    errors: NotRequired[List[str]]  # bounded samples used for pacing and diagnosis
    stored: NotRequired[bool]  # chapters are in the library; no ebook was asked for


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
ADAPTIVE_DECAY = 0.5  # multiply the delay by this after each novel that went fine

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
# A manga is mostly page images, and those are left to their own host (see
# `_images_unpaced`), which is given IMAGE_CONNECTIONS connections. With only
# EXPORT_WORKERS images on the way, a series downloading alone left half of
# them idle; a lane has a thread for each.
LANE_THREADS = max(EXPORT_WORKERS, IMAGE_CONNECTIONS)

# How many novels are downloaded at the same time (extra["parallel_novels"]).
# They share the one request rate, so this does not ask the site for more; it
# fills the gaps one novel leaves while its details page or images load.
DEFAULT_PARALLEL_NOVELS = 2
MAX_PARALLEL_NOVELS = 4

# Defensive idle guard for failures outside normal per-item request budgets.
# The engine drains only a bounded number of requests and stops scheduling a
# novel after repeated failures; partial content remains resumable.
STALL_TIMEOUT = 120.0  # seconds with no completion before giving up on a novel

# "Until done" mode, for a job left alone for days. Instead of stopping when a
# site blocks it or its downloads hang, it waits and tries again, longer each
# time nothing improves, and only finishes once rounds stop getting anywhere.
# Waits after rounds with no progress: 10 min, 30 min, 1, 2, 4 and 8 hours, so a
# site that is down or blocking for most of a day is waited out. These long
# waits only happen once nothing else is left to download.
COOLDOWNS = (600.0, 1800.0, 3600.0, 7200.0, 14400.0, 28800.0)
# Passes a novel may go without improving before it is kept as it is; one more
# than the waits, so every novel is tried again after the longest one.
STUCK_PASSES = len(COOLDOWNS) + 1
MAX_STALL_RECOVERIES = 12  # fresh downloaders after hangs, before calling it broken
STALL_COOLDOWN = 300.0  # wait after replacing a hung lane's downloaders
# Below this much free space for the library, downloads wait for room: on a
# full drive every chapter was fetched and then could not be saved, and the
# export ran on at under a chapter a minute without saying why.
MIN_FREE_SPACE = 256 * 1024 * 1024
DISK_FULL_WAIT = 60.0  # how often to look again while waiting for room
# A novel is not started unless the drive has room for all of it: what its
# chapters are expected to take, going by the novels this export has finished,
# and this much over, since the next one is never quite the average.
ROOM_MARGIN = 1.25
DISCOVERY_MAX_MINUTES = 180.0  # longest a continued discovery round may run
HEARTBEAT = 60.0  # a waiting job still updates itself this often
MAX_RESTARTS = 5  # fresh starts after an unexpected error, before reporting it
DESKTOP_COPY_LIMIT = 2 * 1024 * 1024 * 1024
# Stalls and restarts this long ago are forgotten: the limits are for trouble
# that keeps coming, not for a computer that slept three times in a week.
BUDGET_FORGET = 6 * 3600.0
RESTART_WAIT = 120.0  # seconds before a fresh start (times the number of restarts)

# Library-only runs keep no ebook; this stands in for one in the bookkeeping.
_IN_LIBRARY = Path("<library>")


class NoRoomForNovel(Exception):
    """The drive does not have the room this novel is expected to take."""

    def __init__(self, title: str, need: int, free: int, own: int = 0) -> None:
        super().__init__(f"{title} needs about {need} bytes and {free} are free")
        self.title = title
        self.need = need  # with what the novels downloading beside it still need
        self.own = own or need  # this novel alone
        self.free = free


def _room_needed(chapters: int, bytes_per_chapter: float) -> int:
    """Room a novel with this many chapters still to get is expected to take, with a margin."""
    return int(chapters * bytes_per_chapter * ROOM_MARGIN)


def _folder_size(folder: Path) -> int:
    total = 0
    for parent, _, names in os.walk(folder):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(parent, name))
            except OSError:
                pass
    return total


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


def _run_lanes(count: int, lane: Callable[[int], None]) -> None:
    """Run ``lane(0..count-1)`` side by side and wait for all of them.

    One lane runs right here. When a lane fails the others finish the novel they
    are on and stop; the most telling error is raised (a stall or a real failure
    over the plain "stopped" the other lanes see once the job is cancelled).
    """
    if count <= 1:
        lane(0)
        return
    errors: List[BaseException] = []

    def _run(index: int) -> None:
        try:
            lane(index)
        except BaseException as error:  # handed to the caller below
            errors.append(error)

    threads = [
        threading.Thread(target=_run, args=(index,), name=f"export-lane-{index}", daemon=True)
        for index in range(count)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    if errors:
        errors.sort(key=lambda error: isinstance(error, AbortedException))
        raise errors[0]


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
    except ValueError as error:
        # A checkpoint cut short (a pulled cable leaves it empty) stopped every
        # resume until someone deleted it by hand. Set aside, and carried on
        # without: novels already in the library are found there and skipped.
        aside = path.with_name(f"{path.name}.unreadable-{strftime('%Y%m%d-%H%M%S')}")
        try:
            os.replace(path, aside)
            ctx.logger.warn(
                f"Export checkpoint {path} could not be read ({error}); kept as {aside.name}"
            )
        except OSError:
            pass
        return {}
    except OSError as error:
        raise HandlerException(f"Could not read export checkpoint {path}: {error}") from error


def _in_data_folder(saved: Any) -> Any:
    """``saved``, or the same file in this data folder when it was written down in another.

    Resume records name files by their full path. After the data folder moved
    to another drive (LNCRAWL_DATA_PATH) those paths were gone, and every
    finished novel looked unfinished and was gone over again. Only a path
    through a ``novels`` or ``exports`` folder is followed, and only to a file
    that is there; anything else (a folder chosen for the zip) is left as it is.
    """
    if not isinstance(saved, str) or not saved or Path(saved).exists():
        return saved
    parts = [part for part in re.split(r"[\\/]+", saved) if part]
    for index in reversed(range(len(parts))):
        if parts[index] in ("novels", "exports"):
            candidate = ctx.files.resolve("/".join(parts[index:]))
            if candidate.exists():
                return str(candidate)
    return saved


def _load_manifest(domain: str) -> Dict[str, dict]:
    """Load the resume manifest: ``{novel_url: {"file": str, "missing": int}}``."""
    novels = _load_manifest_file(domain).get("novels")
    if not isinstance(novels, dict):
        return {}
    for record in novels.values():
        if isinstance(record, dict):
            for key in ("file", "archive"):
                if key in record:
                    record[key] = _in_data_folder(record[key])
    return novels


def _load_tuned_rps(domain: str) -> float:
    """The request rate (req/s) the auto-tuner settled on in the last run, or 0."""
    try:
        return float(_load_manifest_file(domain).get("tuned_rps") or 0)
    except Exception:
        return 0.0


def _rate_plan(extra: Dict[str, Any], remembered: float) -> Tuple[float, float, bool]:
    """(rate this run starts at, fastest it may go, whether the stored request is stale).

    ``requests_per_sec`` is the rate the user asked for, a cap; the tuner keeps
    the rate it has reached in ``current_rps``. Before the two were split the
    tuner wrote over the request, so an auto-tuned job that has already run and
    has no ``current_rps`` no longer knows what was asked: what is stored is
    only where it got to, and read as a cap it held a run slowed to 0.2 req/s
    there for good, on every resume and every restart after an error.
    ``remembered`` is the rate the last run on this site finished at.
    """
    auto = bool(extra.get("auto_tune", True))
    asked = float(extra.get("requests_per_sec", 0) or 0)
    reached = float(extra.get("current_rps", 0) or 0)
    if auto and asked > 0 and "current_rps" not in extra and "found_by_discovery" in extra:
        # never below it, though: it was the request itself if the rate never moved
        return asked, max(asked, RPS_MAX), True
    ceiling = asked if asked > 0 else RPS_MAX
    start = asked
    if extra.get("polite") and start <= 0:
        start = POLITE_REQUESTS_PER_SEC
    if auto and reached > 0:
        start = min(reached, ceiling)  # carry on where this job got to
    elif auto and asked <= 0 and remembered > 0:
        start = remembered  # where the last run on this site got to
    return start, ceiling, False


def _images_unpaced(extra: Dict[str, Any]) -> bool:
    """Whether a run's page images are left to their host instead of the run's rate.

    The rate an export keeps is for requests to a site's pages, which is what
    sites limit. Held to it too, a manga's page images came at 5 a second at
    most (the fastest the tuner goes), where the image host answered 47 a
    second with nothing refused. So when the app is choosing the speed, images
    take no turn on the rate: each image host is still paced on its own
    (SitePacer backs off one that refuses) and gets a few connections, not
    more. A rate the user asked for, or polite mode, still holds everything.
    """
    if extra.get("polite") or not extra.get("auto_tune", True):
        return False
    _, _, stale = _rate_plan(extra, 0.0)
    asked = float(extra.get("requests_per_sec", 0) or 0)
    return stale or asked <= 0


def _save_manifest(domain: str, novels: Dict[str, dict], tuned_rps: float = 0) -> None:
    """Persist checkpoints atomically; never silently lose resumability."""
    path = _manifest_path(domain)
    try:
        data = _load_manifest_file(domain)
        previous = data.get("novels")
        data["novels"] = {**(previous if isinstance(previous, dict) else {}), **novels}
        if tuned_rps > 0:
            data["tuned_rps"] = round(tuned_rps, 2)
        path.parent.mkdir(parents=True, exist_ok=True)
        with atomic_write(path) as temporary:
            temporary.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    except Exception as e:
        raise HandlerException(f"Could not save export checkpoint for {domain}: {e}") from e


def _legacy_chapter_limit(novel_url: str, file: Path) -> Optional[int]:
    """Recover scope for old EPUB checkpoints that did not record sample limits."""
    novel = ctx.novels.find_by_url(novel_url)
    if novel is None or not novel.chapter_count:
        return None
    try:
        with zipfile.ZipFile(file) as book:
            count = sum(
                bool(re.search(r"(?:^|/)chapter_\d+\.xhtml$", name)) for name in book.namelist()
            )
        if count >= novel.chapter_count:
            return 0
        return count or None
    except (OSError, zipfile.BadZipFile):
        return None


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
    # ZIPs kept in the data folder, and any written to a folder the user chose
    archives: Set[Path] = set(exports.glob("*.zip"))
    for record in manifest.values():
        saved = record.get("archive") if isinstance(record, dict) else None
        if saved and Path(saved).is_file():
            archives.add(Path(saved))
    for archive in sorted(archives, key=lambda file: file.stat().st_mtime, reverse=True):
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


def _aside(novel_url: str) -> Path:
    """Where a zip-only export keeps a novel's e-book until the zip is made."""
    name = hashlib.sha256(novel_url.encode()).hexdigest()
    return ctx.files.resolve("exports") / "zip-only" / name


def _set_aside(novel_url: str, file: Path) -> Path:
    """Move a finished e-book out of its novel's folder, to outlive the novel there.

    A zip-only export takes each novel out of the library once its e-book is
    made; the book waits by its novel's address for the zip.
    """
    kept = _aside(novel_url)
    kept.mkdir(parents=True, exist_ok=True)
    target = kept / file.name
    if file != target:
        shutil.move(str(file), str(target))
    return target


def _take_out_of_library(novel_url: str) -> bool:
    """Remove the novel at this address from the library; whether one was removed."""
    try:
        novel = ctx.novels.find_by_url(novel_url)
        if novel is None:
            return False
        ctx.novels.delete(novel.id)
        return True
    except Exception as error:
        ctx.logger.warn(f"Could not take {novel_url} out of the library: {error}")
        return False


def _free_space() -> Optional[int]:
    """Bytes free where downloaded books go, or None when it cannot be told.

    That is the book drive when new novels go there: measuring the library's
    own drive held downloads back when the laptop was low, and not at all when
    the drive they were being written to was full.
    """
    books = getattr(ctx, "books", None)
    folder = books.new_books_folder() if books is not None else None
    try:
        return shutil.disk_usage(folder or ctx.files.resolve(".")).free
    except OSError:
        return None


_DISK_FULL_WORDS = (
    "no space left on device",
    "not enough space on the disk",  # Windows
    "database or disk is full",  # SQLite, which raises no OSError for it
)


def _disk_full(error: BaseException) -> bool:
    """Whether ``error`` says the disk is full, however it came to be raised."""
    if isinstance(error, OSError) and error.errno == errno.ENOSPC:
        return True
    text = str(error).lower()
    return any(words in text for words in _DISK_FULL_WORDS)


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


def _name_from_url(novel_url: str) -> str:
    """A readable name from a novel's address, until its real title is known."""
    slug = unquote(novel_url.rstrip("/").rsplit("/", 1)[-1]).split("?", 1)[0]
    words = re.sub(r"[-_]+", " ", slug).strip()
    return words.title() if words else novel_url


def _export_target(save_to: Optional[Path], domain: str, job_id: str) -> Tuple[str, Path]:
    """Where to write the export's zip: (what the job records, the full path).

    In the data folder unless a folder was chosen; there it is named after the
    site, and never written over another file already there.
    """
    if save_to is None:
        stored = f"exports/{job_id}.zip"
        return stored, ctx.files.resolve(stored)
    path = save_to / f"{domain}-novels.zip"
    if path.exists():
        path = save_to / f"{path.stem}-{job_id[:8]}{path.suffix}"
    return str(path), path


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

    _extra_lock = threading.Lock()
    _notified = False

    def _set_extra(self, **values) -> None:
        # Lanes write the job's extra at the same time; it is read, merged and
        # written back, so two unguarded writes would lose one of them.
        with self._extra_lock:
            super()._set_extra(**values)

    def _replace_lane(self, lanes: List, lane: int, url: str) -> None:
        """Swap a lane's crawler for a fresh one, abandoning its hung requests."""
        old = lanes[lane]
        old.taskman.use_limiter(None)  # the limiter is shared; closing must not stop it
        try:
            old.close()
        except Exception as error:
            ctx.logger.debug(f"Closing a hung downloader failed: {error}")
        lanes[lane] = self._new_lane(url)

    def _new_lane(self, url: str):
        """Another crawler for downloading a second novel at once, on the shared rate."""
        lane = ctx.sources.init_crawler(url, workers=EXPORT_WORKERS)
        lane.scraper.signal = self.signal
        lane.taskman.signal = self.signal
        lane.taskman.init_executor(workers=LANE_THREADS, ratelimit=None)
        self.pace.attach(lane.taskman)
        return lane

    def _discover(
        self, crawler, domain: str, budget: float, polite: bool, use_sitemap: bool
    ) -> Tuple[List[str], bool]:
        """Find the site's novels within ``budget`` seconds; (urls, stopped by the cap).

        A site that turns up nothing before the cap is almost certainly blocking
        us. An "until done" job waits and tries again (longer waits each time)
        rather than giving up; otherwise that is a clear failure.
        """
        until_done = bool(self.job.extra.get("until_done"))
        for attempt in range(len(COOLDOWNS) + 1):
            # drive the progress bar with search progress so a slow site isn't a silent 0%
            def _on_discover(done: int, seeds: int, found: int) -> None:
                self._set_extra(phase="discovering", found=found)
                self._set_progress(done, seeds)

            started = monotonic()
            urls = ctx.crawler.discover_novels(
                self.user.id,
                domain,
                self.signal,
                custom=crawler,
                on_progress=_on_discover,
                time_budget=budget,
                delay=POLITE_DELAY if polite else 0,
                use_sitemap=use_sitemap,
            )
            if self.signal.is_set():
                raise AbortedException()
            # Discovery stopping on its time cap is why an export comes back with
            # only part of a source; record it so the UI can say so.
            capped = monotonic() - started >= budget
            self._set_extra(phase="downloading", discovered=len(urls), discovery_capped=capped)
            if urls or not capped:
                if capped:
                    ctx.logger.warn(
                        f"Discovery on {domain} stopped at its {round(budget / 60)} minute cap "
                        f"with {len(urls)} novel(s)"
                        + ("; it carries on once these are downloaded" if until_done else "")
                    )
                return urls, capped
            if not until_done or attempt >= len(COOLDOWNS):
                break
            self._wait(COOLDOWNS[attempt], "cooling-down", cool_reason="blocked-discovery")
        minutes = round(budget / 60)
        raise HandlerException(
            f"Discovery found no novels on '{domain}' within {minutes} minutes — "
            "the source is likely throttling or blocking automated requests. "
            "Try a smaller limit, enable polite mode, or use a different source."
        )

    def _wait(self, seconds: float, phase: str, **extra) -> None:
        """Wait ``seconds``, showing why and when it resumes, and stay alive.

        The job's activity time is refreshed every minute, so a long cool-down
        is never mistaken for a stuck job and cancelled.
        """
        resume_at = current_timestamp() + int(seconds * 1000)
        self._set_extra(phase=phase, resume_at=resume_at, **extra)
        end = monotonic() + seconds
        while True:
            left = end - monotonic()
            if left <= 0:
                break
            if self.signal.wait(min(HEARTBEAT, left)):
                raise AbortedException()
            self._set_extra(phase=phase, resume_at=resume_at)
        self._set_extra(phase="downloading", resume_at=None)

    def _wait_for_room(
        self, need: int = 0, title: str = "", others: Optional[Callable[[], int]] = None
    ) -> None:
        """Hold the export while the library's drive is (nearly) full, until there is room.

        With ``need``, until there is that much: the room the novel called
        ``title`` is expected to take, which is said while it waits. ``others``
        gives what the novels downloading beside it still need, looked at afresh
        each time: it shrinks as they finish.
        """
        own = need
        while True:
            need = own + (others() if others is not None else 0)
            free = _free_space()
            if free is None or free >= MIN_FREE_SPACE + need:
                return
            where = str(ctx.files.resolve("."))
            ctx.logger.warn(f"Only {free // (1024 * 1024)} MB free for {where}; waiting for room")
            self._wait(
                DISK_FULL_WAIT,
                "disk-full",
                disk_free=free,
                disk_path=where,
                disk_need=(MIN_FREE_SPACE + need) if need else None,
                disk_title=title or None,
            )

    def process(self) -> bool:
        until_done = bool((self.job.extra or {}).get("until_done"))
        with keep_awake(enabled=bool((self.job.extra or {}).get("keep_awake", until_done))):
            return self._process_and_notify()

    def _process_and_notify(self) -> bool:
        ok = super().process()
        if not ok and not self._notified:
            try:
                status = ctx.jobs.get(self.job.id).status
            except Exception:
                status = None
            if status not in (JobStatus.FAILED, JobStatus.PAUSED):
                extra = self.job.extra or {}
                done = extra.get("done_novels") or self.job.done or 0
                total = extra.get("total_novels") or self.job.total or 0
                self._notify_finish(
                    f"{self.job.extra.get('domain')}: export stopped after {done} of "
                    f"{total} novel(s). Resume it to carry on where it left off.",
                    title="Lightnovel Crawler — export stopped",
                )
        return ok

    def _set_failure(self, error: str = "", err_source: Optional[Exception] = None) -> bool:
        if not self._notified:
            first = (error or "").strip().splitlines()
            self._notify_finish(
                f"{self.job.extra.get('domain')}: export failed — "
                f"{first[0] if first else 'unknown error'}",
                title="Lightnovel Crawler — export failed",
            )
        # Expected watchdog stops get a short message; worker stacks are saved
        # separately for diagnosis without filling the Activity view.
        if isinstance(err_source, ExportStalledError):
            err_source = None
        return super()._set_failure(error, err_source)

    @staticmethod
    def can_activate(job) -> bool:
        return job.type == JobType.EXPORT_SOURCE

    def run(self) -> None:
        """Run the export; an "until done" one restarts itself after a surprise.

        Something unexpected (a database hiccup, a source bug) would otherwise
        end a job meant to run for days. It waits a little and starts again
        from its checkpoints instead, a few times, before reporting the error.
        Stopping it, and the export's own deliberate failures, still end it.
        """
        restarts = 0
        last_restart = 0.0
        while True:
            try:
                self._run_once()
                return
            except (AbortedException, HandlerException):
                raise
            except Exception as error:
                # The count is of restarts close together. It was for the whole
                # run: five passing hiccups over a week ended a days-long job
                # on the sixth.
                if monotonic() - last_restart > BUDGET_FORGET:
                    restarts = 0
                last_restart = monotonic()
                if not self.job.extra.get("until_done") or restarts >= MAX_RESTARTS:
                    raise
                restarts += 1
                ctx.logger.warn(
                    f"Export hit an unexpected error ({type(error).__name__}: {error}); "
                    f"restarting from its checkpoint ({restarts}/{MAX_RESTARTS})"
                )
                self._set_extra(
                    restarts=restarts, last_restart_error=f"{type(error).__name__}: {error}"[:200]
                )
                self._wait(RESTART_WAIT * restarts, "cooling-down", cool_reason="restarting")

    def _run_once(self) -> None:
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
        until_done = bool(self.job.extra.get("until_done", False))
        library_only = bool(self.job.extra.get("library_only", False))
        # Only the zip is wanted: a novel this export adds to the library is
        # taken out again once its e-book is made. One that was there already
        # is somebody's, and stays.
        zip_only = bool(self.job.extra.get("zip_only", False)) and not library_only
        added: Set[str] = set(self.job.extra.get("zip_only_added") or [])
        # Sitemap mining is what lifts discovery past the one page of results a
        # source's search returns; only turned off when a site's sitemap is junk.
        use_sitemap = bool(self.job.extra.get("use_sitemap", True))
        auto_tune = bool(self.job.extra.get("auto_tune", True))
        update_only = bool(self.job.extra.get("update_only", False))
        # only novels with every chapter downloaded go in the zip; the rest stay
        # in the library as far as they got
        complete_only = bool(self.job.extra.get("complete_only", False))
        # A folder the zip goes to instead of the data folder and the Desktop.
        # Checked before anything downloads: a run can take days, and a drive
        # unplugged since the job was made should say so now, not at the end.
        save_to: Optional[Path] = None
        save_dir = str(self.job.extra.get("save_dir") or "")
        if save_dir and not library_only:
            try:
                save_to = writable_folder(save_dir)
            except (OSError, ValueError) as error:
                raise HandlerException(f"Cannot save the zip to {save_dir}: {error}") from error
        # How fast to fire chapter/image requests: the rate asked for is a cap
        # (polite mode's default is a gentle start); unless one was asked for,
        # start where this job, or else the last run on this site, got to, so a
        # known-touchy site isn't re-blocked while the tuner re-learns.
        remembered = _load_tuned_rps(domain) if auto_tune else 0.0
        requests_per_sec, ceiling, stale = _rate_plan(self.job.extra, remembered)
        if stale:
            # an older job, whose request the tuner overwrote: from here on the
            # request stays as asked (none known) and the tuner keeps its own
            self._set_extra(requests_per_sec=0, current_rps=requests_per_sec)
            ctx.logger.info(f"Resuming at {requests_per_sec} req/s, free to speed up from there")
        elif requests_per_sec and requests_per_sec == remembered:
            ctx.logger.info(f"Starting at remembered rate: {remembered} req/s")
        images_unpaced = _images_unpaced(self.job.extra)
        # every run records it, which is what tells a job from before the split
        self._set_extra(current_rps=requests_per_sec)

        # Seed the de-dupe set with titles already in the library from *other*
        # sources, so a story grabbed from one site isn't redone from another.
        seen_titles: Set[str] = set()
        if dedupe:
            seen_titles = {_norm_title(t) for t in ctx.novels.list_titles(exclude_domain=domain)}

        # Paused or stopped while it was still starting (a worker process can
        # take two minutes to come up): it used to be marked running regardless,
        # and so started itself again, or sat "running" and frozen.
        current = ctx.jobs.get(self.job.id)
        if current.status == JobStatus.PAUSED or current.is_done:
            raise AbortedException()
        self._set_running()

        # One crawler session reused for discovery and every download. The scraper
        # is left unpaced on purpose — the pace controller puts this run's cap on the
        # queue instead, where it can be held exactly with several requests in
        # flight. Pacing is the anti-bot measure too, the fix for "ch 0/N" on
        # Cloudflare-protected sources where unthrottled timing gets the IP
        # blocked, so an uncapped run is still the user asking for that risk.
        crawler = ctx.sources.init_crawler(source.url, workers=EXPORT_WORKERS)
        crawler.scraper.signal = self.signal
        crawler.taskman.signal = self.signal
        crawler.taskman.init_executor(workers=LANE_THREADS, ratelimit=None)
        # One controller holds the rate for the whole run, for every lane, and
        # moves it as each chapter comes back (see export_pace).
        self.pace = PaceController(
            requests_per_sec,
            auto=auto_tune,
            ceiling=ceiling,
            on_change=lambda rate: self._set_extra(current_rps=rate),
        )
        self.pace.attach(crawler.taskman)
        lanes: List = [crawler]
        if requests_per_sec > 0:
            ctx.logger.info(
                f"Export capped at {requests_per_sec} req/s on {domain} "
                f"({EXPORT_WORKERS} in flight)"
            )
        else:
            ctx.logger.info(f"Export uncapped on {domain} ({EXPORT_WORKERS} in flight)")
        try:
            selected = self.job.extra.get("urls")
            # Whether this job found its own novels (so it may look for more
            # later) and whether that search has seen the whole catalogue.
            found_here = bool(self.job.extra.get("found_by_discovery"))
            discovery_done = bool(self.job.extra.get("discovery_done", not found_here))
            budget = float(self.job.extra.get("next_discovery_minutes") or 0) * 60
            budget = budget or discovery_timeout
            if selected:
                # specific novels were picked — export exactly those, so the
                # limit (a cap on how much of a catalogue to take) does not
                # apply and must not silently drop any of them. This is also
                # the resume path: the list persisted below was already capped.
                urls = _select_novels(list(selected))
            else:
                urls, capped = self._discover(
                    crawler, domain, discovery_timeout, polite, use_sitemap
                )
                found_here = True
                urls = _select_novels(urls, limit)
                discovery_done = not capped or bool(limit and len(urls) >= limit)
                budget = discovery_timeout
            if not urls:
                self._set_extra(exported=0, failed=0, total_novels=0)
                # A site that refused every search answered at once, so the
                # search ended well inside its time and the job "succeeded" with
                # nothing, no error and no notice.
                raise HandlerException(
                    f"No novels were found on '{domain}'. The site may be refusing automated "
                    "requests, or its search may have changed. Try again later, turn on "
                    "polite mode, or give the novels' addresses yourself."
                )

            # Replay copies job extras: persist discovery once so Resume does
            # not enumerate a different catalogue or reset its selection.
            self._set_extra(urls=urls, found_by_discovery=found_here, discovery_done=discovery_done)

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
                saved_record = manifest.get(novel_url) if resume else None
                rec = saved_record if isinstance(saved_record, dict) else {}
                done_file = rec.get("file")
                if done_file and "chapter_limit" not in rec and Path(done_file).is_file():
                    recovered_limit = _legacy_chapter_limit(novel_url, Path(done_file))
                    if recovered_limit is not None:
                        rec["chapter_limit"] = recovered_limit
                if (
                    done_file
                    and "chapter_limit" in rec
                    and not (rec or {}).get("missing")
                    and (
                        not (rec or {}).get("chapter_limit")
                        or (max_chapters > 0 and int((rec or {})["chapter_limit"]) >= max_chapters)
                    )
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
            pacing = {"delay": floor_delay}

            # Per-novel missing-chapter count from the previous pass. Used to stop
            # retrying a novel that isn't improving: if a whole pass fills in no new
            # chapters, the rest are unreachable from this source (deleted/blocked
            # pages) and further rounds just waste time and block the batch. A
            # throttled novel that recovers after the backoff shows fewer missing and
            # keeps going; one whose count holds steady is abandoned as partial.
            prev_missing: Dict[str, int] = {}

            # Several novels are downloaded at once, each lane with its own crawler
            # (a source keeps per-novel state on its crawler while reading a novel's
            # page) but all of them sending through the one shared rate.
            parallel = int(self.job.extra.get("parallel_novels") or DEFAULT_PARALLEL_NOVELS)
            parallel = max(1, min(MAX_PARALLEL_NOVELS, parallel))
            book = threading.Lock()  # guards the bookkeeping shared by the lanes
            active: Dict[int, Dict] = {}  # lane -> what it is downloading now
            idle: Dict[int, bool] = {}  # lanes whose novel needed nothing from the site
            needed: List[int] = []  # chapters each started novel still needed
            warmed: Set[int] = set()  # lanes that solved a Cloudflare check already
            stuck: Dict[str, int] = {}  # passes in a row a novel did not improve
            stalls = [0]  # times downloads stopped responding (lately: see BUDGET_FORGET)
            stalled_at = [monotonic()]
            # What a chapter has taken on the drive so far (text, page images and
            # its share of the e-book), kept with the job so a run picked up
            # later knows before its first novel; and what each lane's novel is
            # still expected to take, so two starting together count each other.
            per_chapter = [float(self.job.extra.get("bytes_per_chapter") or 0)]
            measured = [0, 0]  # bytes, chapters
            reserved: Dict[int, int] = {}
            reserved_for: Dict[int, int] = {}  # chapters each reservation was made for

            def _still_reserved(but: int) -> int:
                """Room the novels downloading in the other lanes still need.

                What they have already taken is gone from the free space too:
                counting each one's whole reservation to the end counted it
                twice, and a small novel then waited for room a nearly finished
                50 GB manga beside it no longer needed, without end.
                """
                room = 0
                for other, need in reserved.items():
                    if other == but:
                        continue
                    now = active.get(other) or {}
                    left = max(0, int(now.get("total") or 0) - int(now.get("done") or 0))
                    share = min(1.0, left / reserved_for.get(other, 1)) if now.get("total") else 1.0
                    room += int(need * share)
                return room

            queue: List[str] = []
            last_eta = [0.0]
            # Chapters of each novel (all of them, and those downloaded), from the
            # library to start with and from each novel's own count once it is
            # being downloaded: the progress shown is chapters, not novels, since
            # a novel already downloaded is done in a moment and one of 5,000
            # chapters takes hours. Those not in the library are not known yet.
            try:
                chapter_view: Dict[str, Tuple[int, int]] = ctx.chapters.counts_by_url(urls)
            except Exception as error:
                ctx.logger.debug(f"Could not count the chapters: {error}")
                chapter_view = {}
            if max_chapters > 0:
                chapter_view = {
                    url: (min(total, max_chapters), min(done, max_chapters))
                    for url, (total, done) in chapter_view.items()
                }

            def _chapters() -> Dict[str, Any]:
                totals = [total for total, _ in chapter_view.values()]
                total = sum(totals)
                done = sum(min(d, t) for t, d in chapter_view.values())
                unlisted = sum(1 for url in urls if url not in chapter_view and url not in skipped)
                average = total / len(totals) if totals else 0.0
                return {
                    "chapters_total": total,
                    "chapters_done": done,
                    "novels_unlisted": unlisted,
                    "chapters_guessed": int(unlisted * average),
                }

            def _publish_active(force: bool = False) -> None:
                # called with `book` held
                now = monotonic()
                if not force and now - last_eta[0] < 5:
                    return
                last_eta[0] = now
                counts = _chapters()
                pace = self.pace.pace()
                if counts["chapters_total"] and pace > 0:
                    # every chapter known not to be downloaded, and a guess for
                    # the novels whose chapter lists have not been loaded yet
                    left = counts["chapters_total"] - counts["chapters_done"]
                    eta: Optional[int] = int((left + counts["chapters_guessed"]) / pace)
                else:
                    pairs = [(a["done"], a["total"]) for a in active.values()]
                    average = sum(needed) / len(needed) if needed else 0.0
                    eta = estimate_left(pace, pairs, len(queue), average)
                self._set_extra(
                    **counts,
                    active_novels=[
                        {"title": a["title"], "done": a["done"], "total": a["total"]}
                        for a in active.values()
                    ],
                    eta_seconds=eta,
                    chapters_per_min=round(self.pace.pace() * 60, 1),
                    slowdowns=self.pace.slowdowns(),
                )

            def _process(novel_url: str, lane: int, next_pending: List[str]) -> None:
                lane_crawler = lanes[lane]
                # remember the title for live failure reporting (set once known)
                cur_title: List[str] = [""]
                cached_chapters: List[Optional[int]] = [None]
                chapters_seen = [0]
                idle.pop(lane, None)

                # Shown from the start, by the name in its address, so a novel
                # whose chapter list takes a minute to load is not invisible
                # (and a pause before it does not look like it lasts that long).
                loading = f"{_name_from_url(novel_url)} (loading its chapter list)"
                with book:
                    active[lane] = {"title": loading, "done": 0, "total": 0}
                    self._set_extra(phase="downloading", current_title=loading)
                    _publish_active(force=True)

                # live status so a long run isn't a misleading 0%
                def _on_novel(title: str, c_done: int, c_total: int) -> None:
                    short: Optional[NoRoomForNovel] = None
                    with book:
                        if cached_chapters[0] is None:
                            cached_chapters[0] = c_done
                            if c_done >= c_total:
                                idle[lane] = True  # every chapter here: nothing to ask for
                            needed.append(c_total - c_done)
                            chapters_seen[0] = c_total
                            # Its chapter list is known and nothing of it is
                            # fetched yet: the moment to see whether it fits.
                            need = _room_needed(c_total - c_done, per_chapter[0])
                            free = _free_space()
                            others = _still_reserved(lane)
                            if need and free is not None and free < MIN_FREE_SPACE + need + others:
                                short = NoRoomForNovel(title, need + others, free, own=need)
                            else:
                                reserved[lane] = need
                                reserved_for[lane] = max(1, c_total - c_done)
                    if short is not None:
                        raise short
                    with book:
                        cur_title[0] = title
                        chapter_view[novel_url] = (c_total, c_done)
                        active[lane] = {"title": title, "done": c_done, "total": c_total}
                        self._set_extra(
                            phase="downloading",
                            current_title=title,
                            current_chapters=c_done,
                            current_total_chapters=c_total,
                            current_reused_chapters=cached_chapters[0],
                            done_novels=len(complete) + len(partial) + len(skipped),
                            total_novels=total,
                        )
                        _publish_active()

                def _on_images(i_done: int, i_total: int) -> None:
                    if i_done < i_total:
                        idle.pop(lane, None)  # page images still to get
                    self._set_extra(
                        phase="downloading-images",
                        current_images=i_done,
                        current_total_images=i_total,
                    )

                result: NovelResult
                failure = ""
                try:
                    if zip_only:
                        with book:
                            if novel_url not in added and ctx.novels.find_by_url(novel_url) is None:
                                # written down before it is fetched, and kept with
                                # the job, so a run picked up later still knows
                                # the novel is its own
                                added.add(novel_url)
                                self._set_extra(zip_only_added=sorted(added))
                    result = _download_novel(
                        lane_crawler,
                        self.user.id,
                        novel_url,
                        fmt,
                        self.signal,
                        seen_titles,
                        dedupe,
                        max_chapters,
                        _on_novel,
                        _on_images,
                        self.pace.report,
                        build=not library_only,
                        images_unpaced=images_unpaced,
                    )
                except ExportStalledError:
                    with book:
                        active.pop(lane, None)
                        reserved.pop(lane, None)  # its room is no longer spoken for
                        if monotonic() - stalled_at[0] > BUDGET_FORGET:
                            stalls[0] = 0
                        stalled_at[0] = monotonic()
                        stalls[0] += 1
                        first = stalls[0] == 1
                    if first or not until_done or stalls[0] > MAX_STALL_RECOVERIES:
                        try:
                            diagnostic = _save_stall_diagnostics(self.job.id)
                            self._set_extra(
                                stall_diagnostics=diagnostic, stalled_title=cur_title[0]
                            )
                        except Exception as diagnostic_error:
                            ctx.logger.debug(
                                f"Could not save stall diagnostics: {diagnostic_error}"
                            )
                    if not until_done or stalls[0] > MAX_STALL_RECOVERIES:
                        self.signal.set()  # stop the other lanes too; checkpoints are kept
                        raise
                    # Requests stopped coming back. Give this lane fresh
                    # downloaders (the hung ones are abandoned), let the site rest,
                    # and put the novel back for the next round.
                    ctx.logger.warn(
                        f"Downloads stopped responding on {cur_title[0] or novel_url}; "
                        f"starting fresh downloaders ({stalls[0]}/{MAX_STALL_RECOVERIES})"
                    )
                    self._replace_lane(lanes, lane, source.url)
                    with book:
                        next_pending.append(novel_url)
                        last_error[novel_url] = "Downloads stopped responding; will try again"
                    self._set_extra(stall_recoveries=stalls[0])
                    self._wait(STALL_COOLDOWN, "cooling-down", cool_reason="stalled")
                    return
                except NoRoomForNovel as short:
                    with book:
                        active.pop(lane, None)
                        reserved.pop(lane, None)
                        next_pending.append(novel_url)
                    gb = 1024 * 1024 * 1024
                    said = (
                        f"{short.title} needs about {short.need / gb:.1f} GB and only "
                        f"{short.free / gb:.1f} GB is free on the library's drive"
                    )
                    ctx.logger.warn(f"Not started: {said}")
                    if not until_done:
                        self.signal.set()  # stop the other lanes too; checkpoints are kept
                        raise HandlerException(
                            f"Stopped before the drive filled up: {said}. Move the library to "
                            "a bigger drive or free up space, then resume."
                        ) from short
                    self._wait(
                        DISK_FULL_WAIT,
                        "disk-full",
                        disk_free=short.free,
                        disk_path=str(ctx.files.resolve(".")),
                        disk_need=MIN_FREE_SPACE + short.need,
                        disk_title=short.title,
                    )
                    self._wait_for_room(
                        short.own, short.title, others=lambda: _still_reserved(lane)
                    )
                    return
                except AbortedException:
                    raise
                except Exception as e:
                    reserved.pop(lane, None)
                    if until_done and _disk_full(e):
                        # The disk is full. Nothing downloaded is lost; wait for
                        # space to be freed and try this novel again.
                        with book:
                            active.pop(lane, None)
                            next_pending.append(novel_url)
                        ctx.logger.warn("The disk is full; waiting for free space to continue")
                        self._wait(DISK_FULL_WAIT, "disk-full", disk_free=_free_space())
                        self._wait_for_room()
                        return
                    # Append where it was raised (file:line) so the live UI
                    # pinpoints the source instead of just the message — and
                    # stash the full traceback for the crash log below.
                    frames = traceback.extract_tb(e.__traceback__)
                    where = ""
                    if frames:
                        last = frames[-1]
                        where = f" @ {os.path.basename(last.filename)}:{last.lineno}"
                    failure = f"{type(e).__name__}: {e}{where}"[:200]
                    _log_crash(novel_url, e)
                    ctx.logger.debug(f"Export attempt failed for {novel_url}: {e}")
                    result = {"file": None, "missing": 1, "duplicate": False}

                reserved.pop(lane, None)
                if chapters_seen[0] and (result["file"] is not None or result.get("stored")):
                    # what this novel took, for judging whether the next one fits
                    try:
                        stored_novel = ctx.novels.find_by_url(novel_url)
                        if stored_novel is not None:
                            size = _folder_size(ctx.files.resolve(f"novels/{stored_novel.id}"))
                            with book:
                                measured[0] += size
                                measured[1] += chapters_seen[0]
                                per_chapter[0] = measured[0] / measured[1]
                            self._set_extra(bytes_per_chapter=round(per_chapter[0]))
                    except Exception as error:
                        ctx.logger.debug(f"Could not measure {novel_url}: {error}")

                warm_up = False
                finished = False  # zip only: its e-book is made, so the novel can go
                with book:
                    active.pop(lane, None)
                    if failure:
                        last_error[novel_url] = failure
                    file = result["file"]
                    if (
                        file is not None
                        and result["missing"] == 0
                        and not result["duplicate"]
                        and novel_url in added
                    ):
                        try:
                            file = _set_aside(novel_url, file)
                            finished = True
                        except OSError as error:
                            ctx.logger.warn(f"Could not set aside {file}: {error}")
                    if file is None and result.get("stored"):
                        file = _IN_LIBRARY  # in the library; no ebook was asked for
                    if file is not None and file is not _IN_LIBRARY:
                        manifest[novel_url] = {
                            "file": str(file),
                            "missing": result["missing"],
                            "format": fmt.value,
                            "chapter_limit": max_chapters,
                        }
                        _save_manifest(domain, manifest)
                    if result["duplicate"]:
                        skipped.add(novel_url)  # already have it — don't retry
                        last_error.pop(novel_url, None)
                    elif file is not None and result["missing"] == 0:
                        complete[novel_url] = file
                        partial.pop(novel_url, None)
                        last_error.pop(novel_url, None)
                        pacing["delay"] = max(floor_delay, pacing["delay"] * ADAPTIVE_DECAY)
                    else:
                        give_up = False
                        if file is not None:
                            partial[novel_url] = file  # keep the best version so far
                            # a partial novel has no exception reason; always refresh
                            # it with the latest missing count so retries that fill in
                            # chapters report the current number, not a stale one
                            details = result.get("errors") or []
                            last_error[novel_url] = f"{result['missing']} chapter(s) still missing"
                            if details:
                                last_error[novel_url] += "; " + "; ".join(details)
                            # stop retrying once a pass makes no progress (the missing
                            # chapters are unreachable, not just throttled) — keep the
                            # partial and move on instead of blocking the batch
                            prev = prev_missing.get(novel_url)
                            prev_missing[novel_url] = result["missing"]
                            if prev is not None and result["missing"] >= prev:
                                stuck[novel_url] = stuck.get(novel_url, 0) + 1
                            else:
                                stuck[novel_url] = 0
                            # until done, a novel gets several chances (with the
                            # waits between rounds) before it is kept as it is
                            if stuck[novel_url] >= (STUCK_PASSES if until_done else 1):
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
                        kind = pressure(reason_text)
                        # a failure: slow down (harder if the site refused us). A
                        # partial novel's chapters already told the controller.
                        if file is None and kind:
                            self.pace.report(reason=reason_text)
                        # Only the site pushing back (refusals, timeouts) earns a
                        # longer pause between novels. A novel that is just
                        # missing some chapters says nothing about the site;
                        # it used to add 8s each time, which on a big export left
                        # every lane waiting the full minute after every novel.
                        if kind:
                            bump = ADAPTIVE_STEP * (2 if kind == "refused" else 1)
                            pacing["delay"] = min(
                                ADAPTIVE_MAX, max(pacing["delay"], floor_delay) + bump
                            )
                        else:
                            pacing["delay"] = max(floor_delay, pacing["delay"] * ADAPTIVE_DECAY)
                        # surface the reason live so the UI shows *why*, not just "ch 0"
                        reason = _classify_error(reason_text)
                        self._set_extra(
                            last_fail_title=cur_title[0] or novel_url,
                            last_fail_reason=reason,
                            last_fail_detail=reason_text[:140],
                        )
                        # First time a lane hits anti-bot blocking, solve the
                        # Cloudflare challenge once in a browser and reuse the
                        # clearance for the rest of the run. Best-effort.
                        if lane not in warmed and reason in ("blocked", "anti-bot"):
                            warmed.add(lane)
                            warm_up = True
                    self._set_progress(len(complete) + len(partial) + len(skipped), total)
                    _publish_active(force=True)

                if finished:
                    _take_out_of_library(novel_url)

                warm = getattr(lane_crawler, "warm_up_clearance", None)
                if warm_up and callable(warm):
                    # browser warm-up is best-effort; never let it abort the run
                    try:
                        self._set_extra(phase="solving-challenge")
                        cleared = warm(source.url, self.signal)
                        self._set_extra(phase="downloading", browser_cleared=bool(cleared))
                    except Exception as e:
                        ctx.logger.warn(f"Cloudflare warm-up errored: {e}")

            def _lane(lane: int, next_pending: List[str]) -> None:
                while True:
                    if self.signal.is_set():
                        raise AbortedException()
                    if until_done:
                        self._wait_for_room()  # a novel started on a full disk is thrown away
                    with book:
                        if not queue:
                            return
                        novel_url = queue.pop(0)
                    _process(novel_url, lane, next_pending)
                    # pace the next novel: the delay climbs on failures and decays
                    # on success, so a run eases off when a site pushes back. With
                    # polite mode off and no failures this is 0 (no wait).
                    delay = pacing["delay"]
                    if idle.pop(lane, False):
                        # A novel with everything already here asked the site
                        # for nothing: a resume going past hundreds of finished
                        # novels spent 3 seconds on each in polite mode.
                        delay = 0.0
                    if queue and delay > 0:
                        # polite mode's own pause, or a longer one after errors
                        reason = "errors" if delay > floor_delay else "polite"
                        self._set_extra(
                            phase="waiting-between-novels",
                            adaptive_delay=round(delay, 1),
                            pause_reason=reason,
                        )
                        # listed while it waits, so the card does not look like
                        # one novel at a time when the others are pausing
                        with book:
                            active[lane] = {
                                "title": f"(waiting {round(delay)}s before the next novel)",
                                "done": 0,
                                "total": 0,
                            }
                            _publish_active(force=True)
                        if self.signal.wait(delay):
                            raise AbortedException()
                        with book:
                            active.pop(lane, None)

            def _score() -> Tuple[int, int, int]:
                # what a round can improve: novels finished, chapters still
                # missing in partial ones, novels with nothing at all yet
                return (
                    len(complete) + len(skipped),
                    sum(prev_missing.get(url, 0) for url in partial),
                    len(last_error),
                )

            def _run_passes(pending: List[str]) -> List[str]:
                """The first pass and the retry rounds; returns what is still left.

                Normally a fixed number of retry rounds. "Until done" keeps going
                while rounds make progress, and after a round that made none waits
                (10 minutes, rising to 8 hours) before trying again, so a site that
                blocks for a while is waited out instead of given up on.
                """
                attempt = 0
                quiet = 0  # rounds in a row that improved nothing
                while pending:
                    if attempt > 0:
                        if until_done:
                            if quiet > len(COOLDOWNS):
                                break
                            wait = COOLDOWNS[quiet - 1] if quiet else RETRY_BACKOFF
                            ctx.logger.info(
                                f"Export round {attempt + 1}: {len(pending)} novel(s) left on "
                                f"{domain}; next try in {round(wait / 60)} min"
                            )
                            self._wait(wait, "retry-waiting", retry=attempt, quiet_rounds=quiet)
                        else:
                            if attempt > retries:
                                break
                            ctx.logger.info(
                                f"Export retry {attempt}/{retries}: {len(pending)} novel(s) "
                                f"left on {domain}"
                            )
                            # back off before each retry round so a throttled/blocked
                            # source has time to recover instead of failing again
                            backoff = min(RETRY_BACKOFF * attempt, MAX_RETRY_BACKOFF)
                            self._wait(backoff, "retry-waiting", retry=attempt)
                    before = _score()
                    next_pending: List[str] = []
                    queue[:] = pending
                    while len(lanes) < min(parallel, len(pending)):
                        lanes.append(self._new_lane(source.url))
                    _run_lanes(min(parallel, len(pending)), lambda lane: _lane(lane, next_pending))
                    # keep the original order for the next pass and the summary
                    order = {url: index for index, url in enumerate(urls)}
                    pending = sorted(next_pending, key=lambda url: order.get(url, 0))
                    after = _score()
                    improved = after[0] > before[0] or after[1] < before[1] or after[2] < before[2]
                    quiet = 0 if improved else quiet + 1
                    attempt += 1
                return pending

            pending = _run_passes(pending)

            # A discovery that stopped at its time cap saw only part of the site.
            # Once those are downloaded, an "until done" job looks again with
            # twice the time (up to 3 hours) and downloads whatever is new,
            # until a look finishes inside its time or turns up nothing new.
            while until_done and found_here and not discovery_done and not limit:
                budget = min(DISCOVERY_MAX_MINUTES * 60, budget * 2)
                self._set_extra(next_discovery_minutes=round(budget / 60))
                more, capped = self._discover(crawler, domain, budget, polite, use_sitemap)
                known = set(urls)
                new_urls = [url for url in _select_novels(more) if url not in known]
                discovery_done = not capped or not new_urls
                urls = urls + new_urls
                total = len(urls)
                self._set_extra(
                    urls=urls,
                    discovery_done=discovery_done,
                    total_novels=total,
                    found_more=len(new_urls),
                )
                if not new_urls:
                    break
                ctx.logger.info(f"Found {len(new_urls)} more novel(s) on {domain}")
                self._set_progress(len(complete) + len(partial) + len(skipped), total)
                pending = pending + _run_passes(new_urls)

            # speed memory: remember the rate the tuner settled on for next run
            if auto_tune and self.pace.rate > 0:
                _save_manifest(domain, manifest, tuned_rps=self.pace.rate)

            # bundle the complete ones, plus the best partial we have for the rest.
            # Iterate `partial` directly (not just novels still in `pending`) so a
            # novel we stopped retrying early — because it stopped improving — is
            # still included as an incomplete book rather than silently dropped.
            files: List[Path] = list(complete.values())
            left_out = 0  # partly downloaded, kept out of the zip (complete_only)
            for novel_url, partial_file in partial.items():
                if novel_url in complete:
                    continue
                if complete_only:
                    left_out += 1
                else:
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

            if library_only:
                stored = len(complete) + sum(1 for url in partial if url not in complete)
                incomplete = stored - len(complete)
                self._set_extra(
                    phase="done",
                    library_only=True,
                    exported=stored,
                    complete=len(complete),
                    incomplete=incomplete,
                    failed=failed,
                    skipped=len(skipped),
                    total_novels=total,
                    fail_reasons=fail_reasons,
                    active_novels=[],
                    eta_seconds=None,
                )
                if not stored and failed:
                    raise HandlerException(
                        f"Could not download any of {total} novel(s) from {domain}"
                    )
                self._notify_finish(
                    f"{domain}: {stored} of {total} novel(s) downloaded to your library "
                    f"({len(complete)} complete, {incomplete} partial, {failed} failed, "
                    f"{len(skipped)} skipped)."
                )
                return

            if not files:
                self._set_extra(
                    exported=0,
                    incomplete=0,
                    left_out=left_out,
                    failed=failed,
                    skipped=len(skipped),
                    total_novels=total,
                    fail_reasons=fail_reasons,
                )
                if left_out:
                    message = (
                        f"None of the {total} novel(s) is fully downloaded yet, so there is "
                        f"nothing to put in the zip; {left_out} partly downloaded are in your "
                        "library. Run it again to fill in the missing chapters."
                    )
                    self._notify_finish(f"{domain}: {message}")
                    raise HandlerException(message)
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
            export_rel, export_path = _export_target(save_to, domain, self.job.id)
            export_path.parent.mkdir(parents=True, exist_ok=True)
            # written under a temporary name: a half-written zip is never left
            # looking like a finished one
            writing = export_path.with_name(f"{export_path.name}.part")
            index_entries: List[Tuple[str, str, str]] = []
            zipped = 0
            not_zipped: Set[Path] = set()
            self._set_extra(phase="zipping", zip_done=0, zip_total=len(files))
            try:
                zip_file = zipfile.ZipFile(writing, "w", zipfile.ZIP_DEFLATED)
            except OSError as error:
                raise HandlerException(f"Could not start the zip in {writing.parent}: {error}")
            with zip_file as zf:
                used: set = set()
                for number, file in enumerate(files, start=1):
                    # Stop and Pause used to be ignored for the whole of a
                    # multi-gigabyte zip, with nothing saying it was being made.
                    if self.signal.is_set():
                        zf.close()
                        writing.unlink(missing_ok=True)
                        raise AbortedException()
                    if number % 5 == 0:
                        self._set_extra(phase="zipping", zip_done=number, zip_total=len(files))
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
                        if _disk_full(e):
                            # No room for the zip: said as that. It used to count
                            # as a surprise, and the whole export started again
                            # from the top, five times, before failing.
                            zf.close()
                            writing.unlink(missing_ok=True)
                            raise HandlerException(
                                f"The drive filled up while making the zip in {writing.parent}. "
                                "Every novel is still in the library: free up space or choose "
                                "another folder for the zip, then run it again."
                            ) from e
                        ctx.logger.warn(f"Skipping {file} in export zip: {e}")
                        not_zipped.add(file)
                        continue
                    zipped += 1
                    for record in manifest.values():
                        if record.get("file") == str(file):
                            record.update(archive=str(export_path), archive_name=arcname)
                    status = "incomplete" if file in partial_paths else "complete"
                    index_entries.append((arcname, file.stem, status))
                # a clickable offline index of everything in the archive
                zf.writestr("index.html", _build_index_html(domain, index_entries))
            if not zipped:
                writing.unlink(missing_ok=True)
                raise HandlerException("Could not add any novel to the export zip")
            os.replace(writing, export_path)
            _save_manifest(domain, manifest)

            if zip_only:
                # The zip is made: what this export added and did not finish
                # (partly downloaded, or failed) goes too, and the books set
                # aside for the zip are in it now. A later run finds them there.
                self._set_extra(phase="clearing-library")
                kept_aside = {file.parent for file in not_zipped}
                for url in sorted(added):
                    _take_out_of_library(url)
                    # a book that could not be put in the zip is the only copy
                    # left of it: its folder stays
                    if _aside(url) not in kept_aside:
                        shutil.rmtree(_aside(url), ignore_errors=True)

            # report counts from what actually made it into the zip, so a file
            # that couldn't be written isn't silently counted as complete
            zipped_complete = sum(1 for _, _, s in index_entries if s == "complete")
            zipped_incomplete = len(index_entries) - zipped_complete
            zip_skipped = len(files) - zipped

            export_name = f"{domain}-novels.zip"
            if save_to is not None:
                # already where it was asked to go; another copy is the space
                # the folder was chosen to save
                export_name = export_path.name
                saved_to: Optional[str] = str(export_path)
            else:
                # copied to the Desktop only when small: it is on the system
                # drive, the one a big library was moved away from
                small = export_path.stat().st_size < DESKTOP_COPY_LIMIT
                saved_to = _copy_to_desktop(export_path, export_name) if small else None

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
                left_out=left_out,
                total_novels=total,
                fail_reasons=fail_reasons,
                **({"taken_out": len(added)} if zip_only else {}),
            )

            size_mb = export_path.stat().st_size / (1024 * 1024)
            summary = (
                f"{domain}: {zipped} of {total} novel(s) exported "
                f"({zipped_complete} complete, {zipped_incomplete} partial, {failed} failed, "
                f"{len(skipped)} skipped) — {size_mb:.1f} MB."
            )
            if zip_skipped:
                summary += f" {zip_skipped} could not be added to the zip."
            if left_out and zip_only:
                summary += f" {left_out} partly downloaded left out."
            elif left_out:
                summary += f" {left_out} partly downloaded left out (they are in your library)."
            if zip_only:
                summary += " Nothing it downloaded was kept in your library."
            if saved_to and save_to is not None:
                summary += f" Saved to {saved_to}."
            elif saved_to:
                summary += " Saved to your Desktop."
            self._notify_finish(summary)
        finally:
            for lane_crawler in lanes:
                lane_crawler.close()

    def _set_progress(self, done: int, total: int) -> None:
        with ctx.db.session() as sess:
            ctx.jobs._update(sess, self.job.id, done=done, total=max(total, 1))
            sess.commit()
        self.job.done = done
        self.job.total = max(total, 1)
        ctx.job_notifier.notify(self.user, self.job)

    def _notify_finish(
        self, summary: str, title: str = "Lightnovel Crawler — export finished"
    ) -> None:
        """Fire the configured desktop/webhook notifications, once per run (best-effort)."""
        self._notified = True
        cfg = ctx.config.notify
        if not cfg.desktop_toast and not cfg.webhook_url:
            return
        try:
            notify_finished(
                title=title,
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
    on_images: Optional[Callable[[int, int], None]] = None,
    on_result: Optional[Callable[..., None]] = None,
    build: bool = True,
    images_unpaced: bool = False,
) -> NovelResult:
    """Download a novel and build its ebook.

    Returns the built file (or None) and how many chapters are still missing.
    Already-downloaded chapters are skipped, so calling this again only fills in
    what's still missing — which is what makes the retry passes cheap. If
    ``dedupe`` is on and the (normalized) title is already in ``seen_titles``,
    the novel is reported as a duplicate without downloading anything.
    ``max_chapters`` (>0) caps how many chapters are pulled per novel so one huge
    series can't stall the whole run. ``on_novel(title, done, total)`` reports
    live chapter progress, and ``on_images(done, total)`` the page images after
    them — for a long manga that is thousands of images and most of the time.
    ``on_result(error, count=True)`` hears about every chapter and image as it
    finishes, which is what the request rate is tuned from. ``build`` False
    stops once the chapters are in the library, without making an ebook.
    ``images_unpaced`` sends page images without a turn on the request rate
    (see `_images_unpaced`).
    """
    # Reuse already-fetched metadata: on retry rounds (and re-runs) the novel and
    # its chapter list are already in the DB, so skip re-downloading the info page
    # — a wasted network round-trip per novel per round. Only fetch when new.
    #
    # A stored novel without a synopsis was fetched again too, to fill it in;
    # but a page with no synopsis to find stays without one, and every run and
    # every resume read its whole chapter list again: on NovelFire a page a
    # second, four novels at once "loading their chapter list" for minutes
    # with their chapters already here.
    novel = ctx.novels.find_by_url(novel_url)
    if novel is None or not ctx.chapters.list_ids(novel_id=novel.id):
        novel = ctx.crawler.fetch_novel(user_id, novel_url, signal=signal, custom=crawler)

    norm = _norm_title(novel.title)
    if dedupe and seen_titles is not None and norm in seen_titles:
        return {"file": None, "missing": 0, "duplicate": True}
    books = getattr(ctx, "books", None)
    if books is not None and books.unavailable(novel.id):
        # every chapter would look missing, be fetched and fail to save
        from ....services.books import BookDriveUnplugged

        raise BookDriveUnplugged(
            f"{novel.title} is on the book drive ({books.folder()}), which is not plugged in"
        )

    # first N chapters when capped, so a giant series doesn't eat the whole run
    chapter_ids = ctx.chapters.list_ids(novel_id=novel.id, limit=max_chapters or None)
    if not chapter_ids:
        return {"file": None, "missing": 0, "duplicate": False}

    # download the chapters (fetch_chapter skips ones already fetched), reporting
    # progress as each one completes
    pending_chapters = ctx.crawler.pending_chapter_ids(chapter_ids)
    c_total = len(chapter_ids)
    c_cached = c_total - len(pending_chapters)
    if on_novel:
        on_novel(novel.title, c_cached, c_total)
    last_emit = [monotonic()]
    errors: List[str] = []

    def _tick(done: int) -> None:
        # report at least every ~2s (and at the end) so the UI reflects real
        # movement instead of looking frozen on a big, slow novel
        if on_novel and (done + c_cached == c_total or monotonic() - last_emit[0] >= 2.0):
            last_emit[0] = monotonic()
            on_novel(novel.title, done + c_cached, c_total)

    # drain with the stall watchdog so one hung request can't freeze the run
    try:
        for outcome in crawl_items(
            pending_chapters,
            lambda cid: crawler.taskman.submit_task(
                ctx.crawler.fetch_chapter,
                user_id,
                cid,
                signal=signal,
                custom=crawler,
            ),
            signal,
            max_inflight=EXPORT_WORKERS,
            idle_timeout=STALL_TIMEOUT,
            on_progress=_tick,
        ):
            if on_result:
                on_result(outcome.error)
            if outcome.error is not None and _disk_full(outcome.error):
                # every chapter after it would be fetched and thrown away as well
                raise OSError(errno.ENOSPC, f"No room to save chapters: {outcome.error}")
            if outcome.error is not None and len(errors) < 3:
                errors.append(f"{type(outcome.error).__name__}: {outcome.error}"[:300])
    except CrawlStalledError as error:
        raise ExportStalledError(str(error)) from error

    # download chapter images in one batch (covers manga pages and inline art);
    # one query for the whole novel instead of one per chapter
    image_ids = ctx.images.list_ids(novel_id=novel.id)
    if image_ids:
        pending_images = ctx.crawler.pending_image_ids(image_ids)
        i_total = len(image_ids)
        i_cached = i_total - len(pending_images)
        if on_images and pending_images:
            on_images(i_cached, i_total)

        def _image_tick(done: int) -> None:
            if on_images and (done + i_cached == i_total or monotonic() - last_emit[0] >= 2.0):
                last_emit[0] = monotonic()
                on_images(done + i_cached, i_total)

        # Only images on an image host of their own skip the site's rate; those
        # on the site's own host take their turn like its pages, and only those
        # tell the rate how the site is doing.
        own: Set[str] = set(pending_images)
        if images_unpaced:
            own = set()
            for offset in range(0, len(pending_images), 500):
                for image in ctx.images.get_many(pending_images[offset : offset + 500]):
                    if is_own_host(crawler, image.url):
                        own.add(image.id)

        def send_image(fn, *args, **kwargs):
            image_id = args[1]
            if image_id in own:
                return crawler.taskman.submit_task(fn, *args, **kwargs)
            return crawler.taskman.submit_unpaced(fn, *args, **kwargs)

        try:
            for outcome in crawl_items(
                pending_images,
                lambda image_id: send_image(
                    ctx.crawler.fetch_image,
                    user_id,
                    image_id,
                    signal=signal,
                    custom=crawler,
                ),
                signal,
                # left to their host, as many as it is given connections; held
                # to the export's rate, as many as chapters
                max_inflight=IMAGE_CONNECTIONS if images_unpaced else EXPORT_WORKERS,
                idle_timeout=STALL_TIMEOUT,
                on_progress=_image_tick,
            ):
                if on_result and outcome.item_id in own:
                    on_result(outcome.error, count=False)
        except CrawlStalledError as error:
            raise ExportStalledError(str(error)) from error

    # how many of the targeted chapters are still not downloaded (drives retries)
    # Old completion flags can outlive blank or missing content files. Inspect
    # the targeted files so those gaps stay eligible for the next retry pass.
    missing = len(ctx.crawler.pending_chapter_ids(chapter_ids))
    available = len(chapter_ids) - missing
    if not available:
        reason = "No chapter text was downloaded; refusing to export an empty ebook"
        if errors:
            reason += "; " + "; ".join(errors)
        raise HandlerException(reason)

    if not build:
        if seen_titles is not None and missing == 0:
            seen_titles.add(norm)
        return {
            "file": None,
            "missing": missing,
            "duplicate": False,
            "errors": errors,
            "stored": True,
        }

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
        if seen_titles is not None and missing == 0:
            seen_titles.add(norm)  # remember it so other sources skip this story
    return {"file": file, "missing": missing, "duplicate": False, "errors": errors}
