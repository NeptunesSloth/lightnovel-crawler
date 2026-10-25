"""Process-wide scraper configuration and shared state.

lncrawl-scraper 1.x keeps what it learns about a site on disk — which detection
layer binds it, how fast the site tolerates being asked, and any challenge
clearance a browser earned — in a ``Memory``, and shares exit addresses and
pacing through a ``SharedState``. Both describe the *site*, not the object
talking to it, so they belong to the process rather than to one crawler.

That is not a preference. Every ``Memory`` holds all the origins it knows and a
flush writes the whole file, so two stores over one path never merge: the later
write is the entire file and whatever the other one learned is gone. A
``Crawler`` is built per source and ``ctx.http`` keeps a scraper of its own, so
letting each construct its own state would quietly destroy the saved clearances
— the thing persistence was added for. Everything here is therefore built once
and handed to every ``Scraper``.

Sharing also buys correctness the per-crawler arrangement could not have: one
exit pool, and pacing that is per-origin across the whole process instead of
per-crawler, so two jobs on one site no longer pace themselves independently
into a block.
"""

import logging
import re
import threading
from typing import Dict, List, Optional

from scraper import (
    BrowserSolver,
    ExitKind,
    ExitSpec,
    PacingPolicy,
    Scraper,
    ScraperConfig,
    SharedState,
    diagnosis,
)
from scraper.memory import Memory

from ..context import ctx
from ..exceptions import AbortedException
from .request_budget import current_budget

logger = logging.getLogger(__name__)

# An unlabelled proxy URL from the config gets this kind. The pool ranks exits by
# kind and `explain()` reports which of them can clear a layer, so the label has
# to be a claim we can stand behind: almost every proxy someone pastes in is a
# datacenter address, and overstating it as residential would have the diagnosis
# blame the site for a block the address caused.
DEFAULT_EXIT_KIND = ExitKind.DATACENTER

# The scraper spaces requests per origin, and that spacing — not the number of
# download workers — is what sets throughput: a worker that is not allowed to
# send yet simply waits somewhere else. Measured against a local server, raising
# the worker count alone moved 1.8x while lowering the interval moved 9.2x.
#
# Kept above zero even when nothing is throttling us, because the adaptive
# backoff multiplies the interval in force: from a literal zero there is nothing
# to grow, and a site that starts pushing back could never be backed away from.
PACING_FLOOR = 0.05

# A JavaScript-only redirect page names its destination in a JS string, and pages
# that build that string with a JSON encoder escape the slashes: "https:\/\/host\/x".
# lncrawl-scraper 1.9.0 follows the text as written, so urljoin reads it as a path on
# the redirecting host and the hop lands on a 404. NovelFire now sends every chapter
# through such a page, which left each one empty. Decoding the escapes first gives
# the address the page actually sends a browser to.
_JS_ESCAPE = re.compile(r"\\(?:u([0-9a-fA-F]{4})|x([0-9a-fA-F]{2})|(.))", re.DOTALL)
_find_js_redirect = diagnosis.js_redirect


def _unescape_js(match: "re.Match[str]") -> str:
    code = match.group(1) or match.group(2)
    return chr(int(code, 16)) if code else match.group(3)


def _js_redirect(body: str) -> str:
    return _JS_ESCAPE.sub(_unescape_js, _find_js_redirect(body))


diagnosis.js_redirect = _js_redirect

_lock = threading.RLock()
_memory: Optional[Memory] = None
_configs: Dict[float, ScraperConfig] = {}
_states: Dict[float, SharedState] = {}


def pacing_for(requests_per_sec: float) -> PacingPolicy:
    """Pacing policy for a target request rate. ``0`` or less means unpaced.

    The library's own default is a 3 second mean interval — right for an
    unattended scrape of a protected site, and roughly nine times slower than
    what someone who picked a rate asked for.
    """
    if requests_per_sec <= 0:
        return PacingPolicy(interval=0.0, floor=PACING_FLOOR)
    interval = 1.0 / requests_per_sec
    # The floor clamps the short tail of the gap distribution. Left at its
    # default it becomes the real limit for any rate above about 3/s, so it has
    # to scale with the interval it is a floor for.
    return PacingPolicy(interval=interval, floor=min(0.35, interval / 4))


def _parse_exit(entry: str) -> Optional[ExitSpec]:
    """One ``proxy_urls`` entry as an :class:`ExitSpec`, or None if unusable.

    Accepts a bare URL, ``<kind>;<url>`` to declare what the address is, and the
    legacy ``tor;host;port[;control_port;control_password]`` form.
    """
    entry = entry.strip()
    if not entry:
        return None

    head, _, rest = entry.partition(";")
    if not rest:
        return ExitSpec(url=entry, kind=DEFAULT_EXIT_KIND)

    try:
        kind = ExitKind(head.strip().lower())
    except ValueError:
        # not a kind prefix — a URL that merely contains a ';'
        return ExitSpec(url=entry, kind=DEFAULT_EXIT_KIND)

    if kind is ExitKind.TOR and not rest.startswith(("socks", "http")):
        # legacy form: tor;host;port[;control_port;control_password]
        parts = [p.strip() for p in rest.split(";")]
        host = parts[0] if parts else ""
        port = parts[1] if len(parts) > 1 else "9050"
        if not host:
            return None
        if len(parts) > 2:
            # 1.x drops control-port rotation: a new circuit is no longer
            # requested over Tor's control port, so these fields do nothing.
            logger.warning(
                "Tor control-port rotation is no longer supported; "
                f"ignoring the control port and password for {host}"
            )
        return ExitSpec(url=f"socks5h://{host}:{port}", kind=ExitKind.TOR)

    return ExitSpec(url=rest.strip(), kind=kind)


def _exits() -> List[ExitSpec]:
    """The configured exit addresses, in config order.

    An empty list is a direct connection. 1.x has no "fall back to direct when a
    proxy misses" switch — a silent fallback hid why a scrape failed — so the
    equivalent is a direct exit listed explicitly alongside the proxies.
    """
    if not ctx.config.crawler.enable_proxy:
        return []

    exits: List[ExitSpec] = []
    for entry in ctx.config.crawler.proxy_urls.split(","):
        spec = _parse_exit(entry)
        if spec is not None:
            exits.append(spec)

    if exits and ctx.config.crawler.allow_fallback_on_proxy_miss:
        exits.append(ExitSpec(kind=ExitKind.DIRECT))
    return exits


def _browser_solver() -> Optional[BrowserSolver]:
    """A browser to clear challenges with, or None when browsing is off.

    1.x dropped the in-process Cloudflare solvers, so a managed challenge is only
    passable by driving a real browser. Without a solver configured the scraper
    has nothing to escalate to and a protected source simply fails.
    """
    if not ctx.config.crawler.can_use_browser:
        return None
    try:
        from scraper import CdpSolver, find_chromium

        from ..utils.browser_detect import pick_executable

        executable = pick_executable() or next(iter(find_chromium()), None)
        # `headless=False` means "auto" in 1.x (hidden first, a window only once
        # the unattended attempt failed), so say which one we mean.
        mode = "headless" if ctx.config.crawler.use_headless_mode else "auto"
        return CdpSolver(executable=executable, mode=mode)
    except Exception:
        logger.warning("No browser solver available; challenges cannot be cleared")
        return None


def _build_config(requests_per_sec: float) -> ScraperConfig:
    return ScraperConfig(
        exits=_exits(),
        browser=_browser_solver(),
        pacing=pacing_for(requests_per_sec),
        data_dir=ctx.config.app.app_dir / "scraper",
        # Sources read `response.status_code` and branch on it themselves, the way
        # they did when the scraper never raised for one. Leaving this on would
        # turn every such check into a `requests.HTTPError` from inside the call.
        raise_for_status=False,
    )


def _rate_key(requests_per_sec: Optional[float]) -> float:
    """Normalise a rate into a cache key. ``None`` means the configured default."""
    if requests_per_sec is None:
        requests_per_sec = ctx.config.crawler.requests_per_sec
    return max(0.0, round(float(requests_per_sec), 4))


def memory() -> Memory:
    """The one store of what we know about each site, for the whole process."""
    global _memory
    with _lock:
        if _memory is None:
            _memory = Memory(_build_config(0.0).memory_path)
        return _memory


def scraper_config(requests_per_sec: Optional[float] = None) -> ScraperConfig:
    """The scraper configuration for a request rate."""
    rate = _rate_key(requests_per_sec)
    with _lock:
        config = _configs.get(rate)
        if config is None:
            config = _build_config(rate)
            _configs[rate] = config
            logger.debug(
                f"Scraper configured for {rate or 'unpaced'} req/s with {len(config.exits)} exit(s)"
            )
        return config


def shared_state(requests_per_sec: Optional[float] = None) -> SharedState:
    """The state scrapers on this rate share: one memory, one exit pool, one pacer.

    There is a state per distinct rate because the rate lives in the pacer's
    policy, and that policy is the floor on the interval — the library will slow
    an origin down from there but never speed it past what was asked for. All of
    them are built over the single process-wide ``Memory``, which is the part that
    must not be duplicated: each store holds every origin it knows and a flush
    writes the whole file, so two over one path do not merge.
    """
    rate = _rate_key(requests_per_sec)
    with _lock:
        state = _states.get(rate)
        if state is None:
            state = SharedState.create(scraper_config(rate), memory=memory())
            _states[rate] = state
        return state


def new_scraper(
    origin: str = "",
    parser: Optional[str] = None,
    requests_per_sec: Optional[float] = None,
) -> Scraper:
    """A scraper sharing this process's memory, exit pool and pacing for its rate."""
    rate = _rate_key(requests_per_sec)
    scraper = Scraper(
        origin=origin,
        config=scraper_config(rate),
        parser=parser,
        state=shared_state(rate),
    )
    original_fetch = scraper.fetch

    def bounded_fetch(method, url, **kwargs):
        budget = current_budget()
        if budget is None:
            return original_fetch(method, url, **kwargs)
        remaining = budget.remaining()
        previous_signal = kwargs.get("signal")

        class RetrievalSignal:
            def is_set(self) -> bool:
                return budget.is_set() or bool(previous_signal and previous_signal.is_set())

        kwargs["signal"] = RetrievalSignal()
        configured = kwargs.get("timeout") or scraper.config.timeout
        if isinstance(configured, (int, float)):
            configured = (configured, configured)
        connect, read = configured if isinstance(configured, tuple) else (10, 20)
        kwargs["timeout"] = (
            min(float(connect or 10), 10, remaining),
            min(float(read or 20), 20, remaining),
        )
        try:
            response = original_fetch(method, url, **kwargs)
        except AbortedException:
            budget.remaining()  # Distinguish a chapter timeout from job cancellation.
            raise
        budget.remaining()
        return response

    setattr(scraper, "fetch", bounded_fetch)
    if rate > 0 and origin:
        _hold_rate(scraper, origin, 1.0 / rate)
    return scraper


def _hold_rate(scraper: Scraper, origin: str, interval: float) -> None:
    """Make sure this origin's remembered interval is no faster than ``interval``.

    The policy is the floor the library narrows towards, but a fetch re-seeds the
    pacer from the origin's profile first, and that profile is shared by every
    rate in the process — so an interval learned while running uncapped would
    otherwise undercut a run that asked to be slower. Raising it here, and never
    lowering it, keeps the cap a cap and incidentally remembers it for next run.
    """
    profile = scraper.memory.profile(origin)
    if profile.interval >= interval:
        return
    profile.interval = interval
    scraper.memory.touch()
    scraper.pacer.learn(scraper.memory.key(origin), interval)


def close_shared_state() -> None:
    """Flush learned state to disk. Safe to call more than once."""
    global _memory
    with _lock:
        _states.clear()
        _configs.clear()
        if _memory is None:
            return
        try:
            _memory.close()
        except Exception:
            logger.debug("Could not flush scraper memory", exc_info=True)
        _memory = None
