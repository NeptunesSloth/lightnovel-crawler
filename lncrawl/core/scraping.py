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
import threading
from typing import List, Optional

from scraper import (
    BrowserSolver,
    ExitKind,
    ExitSpec,
    Scraper,
    ScraperConfig,
    SharedState,
)

from ..context import ctx

logger = logging.getLogger(__name__)

# An unlabelled proxy URL from the config gets this kind. The pool ranks exits by
# kind and `explain()` reports which of them can clear a layer, so the label has
# to be a claim we can stand behind: almost every proxy someone pastes in is a
# datacenter address, and overstating it as residential would have the diagnosis
# blame the site for a block the address caused.
DEFAULT_EXIT_KIND = ExitKind.DATACENTER

_lock = threading.RLock()
_config: Optional[ScraperConfig] = None
_state: Optional[SharedState] = None


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


def _build_config() -> ScraperConfig:
    return ScraperConfig(
        exits=_exits(),
        browser=_browser_solver(),
        data_dir=ctx.config.app.app_dir / "scraper",
        # Sources read `response.status_code` and branch on it themselves, the way
        # they did when the scraper never raised for one. Leaving this on would
        # turn every such check into a `requests.HTTPError` from inside the call.
        raise_for_status=False,
    )


def scraper_config() -> ScraperConfig:
    """The process-wide scraper configuration."""
    global _config
    with _lock:
        if _config is None:
            _config = _build_config()
            logger.debug(f"Scraper configured with {len(_config.exits)} exit(s)")
        return _config


def shared_state() -> SharedState:
    """The one ``SharedState`` every scraper in this process must be built with."""
    global _state
    with _lock:
        if _state is None:
            _state = SharedState.create(scraper_config())
        return _state


def new_scraper(origin: str = "", parser: Optional[str] = None) -> Scraper:
    """A scraper sharing this process's configuration, memory and exit pool."""
    return Scraper(
        origin=origin,
        config=scraper_config(),
        parser=parser,
        state=shared_state(),
    )


def close_shared_state() -> None:
    """Flush learned state to disk. Safe to call more than once."""
    global _state
    with _lock:
        if _state is None:
            return
        try:
            _state.memory.close()
        except Exception:
            logger.debug("Could not flush scraper memory", exc_info=True)
        _state = None
