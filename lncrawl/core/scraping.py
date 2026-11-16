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

from collections import deque
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import asdict
import logging
import math
import threading
import time
from typing import Any, Callable, Deque, Dict, Iterator, List, Optional, Set, Tuple

from lxml import html as lxml_html
from scraper import (
    BrowserSolver,
    ExitKind,
    ExitSpec,
    Pacer,
    PacingPolicy,
    Scraper,
    ScraperConfig,
    SharedState,
    diagnosis,
    session as scraper_session,
)
from scraper.memory import Memory, OriginProfile

from ..context import ctx
from ..exceptions import AbortedException
from .request_budget import current_budget, waiting_turn

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
_find_js_redirect = diagnosis.js_redirect


def _js_redirect(body: str) -> str:
    # ponytail: only JSON's escaped slash; decode \uXXXX too if a site ever sends it.
    return _find_js_redirect(body).replace("\\/", "/")


diagnosis.js_redirect = _js_redirect

# The scraper checks each page's visible words against the site's usual ones to
# spot block and challenge pages, and gets them by building a second
# BeautifulSoup tree of every page it fetches. Profiled on a server download that
# was a third of all its CPU, more than parsing the page for the crawler itself.
# Walking an lxml tree gives the same words, script/style/noscript/template and
# comments left out, about nine times faster.
_slow_visible_text = scraper_session._visible_text
_INVISIBLE = frozenset({"script", "style", "noscript", "template"})


# The scraper asks for a page's words twice in a row (to check it, then to
# learn from it), passing the same string both times: keep the last answer.
_last_visible = threading.local()


def _visible_text(html: str, parser: str) -> str:
    last = getattr(_last_visible, "entry", None)
    if last is not None and last[0] is html:
        return last[1]
    text = _extract_visible_text(html, parser)
    _last_visible.entry = (html, text)
    return text


def _extract_visible_text(html: str, parser: str) -> str:
    try:
        root = lxml_html.document_fromstring(html)
    except Exception:  # empty or unparseable: the original copes with both
        return _slow_visible_text(html, parser)
    parts: List[str] = []
    stack: List[Tuple[Any, bool]] = [(root, False)]
    while stack:
        element, closing = stack.pop()
        if closing:
            if element.tail and element is not root:
                parts.append(element.tail)
            continue
        stack.append((element, True))
        # comments and processing instructions have a non-string tag: only their tail is text
        if isinstance(element.tag, str) and element.tag not in _INVISIBLE:
            if element.text:
                parts.append(element.text)
            stack.extend((child, False) for child in reversed(element))
    return " ".join(part.strip() for part in parts if part.strip())


scraper_session._visible_text = _visible_text

# The longest gap a slowdown remembered from an earlier run may impose on the
# first requests of this one. A site that really needs longer refuses again
# and the gap grows from here, so this costs a refusal or two at most.
CARRY_OVER_MAX = 2.0
# Refusals closer together than this are one burst: the requests already in
# flight when a site's window closed, all answered the same way.
REFUSAL_BURST = 1.0
# The longest a site's "come back later" holds every request to it. Held for a
# 4-minute Retry-After, nothing completed for two and the export's watchdog
# stopped the run. After this, requests go again: a site still closed refuses
# them, and the request that was refused still waits the rest out itself.
MAX_SITE_BLOCK = 60.0
# A wait longer than one block is a closure, not a counting window: refusals
# until it ends are that same closure, and slow nothing further. Remembered at
# most this long, so a nonsense Retry-After cannot hold a site's pace for good.
MAX_CLOSURE = 3600.0
# How far under the pace a site's counter allows to go. A site that lets ten
# requests through and then says "wait ten seconds" takes ten per ten seconds;
# 15% under that leaves room for requests not arriving perfectly evenly.
LIMIT_MARGIN = 1.15
# The least a refusal widens the gap. A second refusal this soon after the
# last one means that correction was not enough, and doubles it instead.
REFUSED_STEP = 1.08
REPEAT_WINDOW = 60.0
# Easing stops this far short of a pace that was refused, for this long; after
# that it may try faster again, in case the limit was relaxed or the refusal
# was someone else's doing (a browser open on the same site).
TOO_FAST_MARGIN = 1.05
TOO_FAST_TTL = 600.0
# How uneven the gaps to a counting site are: enough not to tick like a clock,
# little enough that its window never holds one request more than planned.
EVEN_JITTER = 0.1
# How many recent successes per site are remembered, to count what a window held.
PASSED_KEPT = 512


class SitePacer(Pacer):
    """Per-site request pacing that finds a site's limit and stays just under it.

    Measured on NovelFire and its chapter mirror: each host lets ten requests
    through, answers the eleventh with 429 and ``Retry-After: 10``, and refuses
    everything for those ten seconds. The two hosts count separately, and a
    steady 0.9 requests a second to both at once is never refused: about 54
    chapters a minute. An export got 15 per novel being downloaded, because
    the mirror was refusing 4 requests in 10 and every refusal parks a download
    thread for its ten seconds. Three things in the library's pacer cause that:

    - Each waiting thread times itself from the last request sent, so two go
      out per gap, and the gaps are deliberately uneven, so a ten-second window
      regularly holds an eleventh request.
    - A refusal doubles the gap and successes ease it back 10% at a time, with
      nothing to say where the limit was, so it keeps walking back into it.
    - Requests already waiting go out while the site is still saying wait, and
      are refused too.

    (It also took Retry-After as the new gap and could not recover from it,
    because every fetch re-reads the gap saved on disk; that was fixed first.)

    Here a thread reserves the site's next free turn before it sleeps, so the
    pace holds however many threads wait. A refusal that names a wait says what
    the site counts, the requests it let through in that time: the gap is set
    LIMIT_MARGIN under that at once, and nothing is sent to the site until the
    wait is over. A site that has refused this way is paced evenly from then
    on. Successes ease the gap as in the library, but never back to a pace that
    was refused (TOO_FAST_MARGIN, for TOO_FAST_TTL).

    A gap remembered from an earlier run seeds a site once, capped at
    CARRY_OVER_MAX, and is never shorter than the rate the policy was made for.
    """

    def __init__(
        self, policy: Optional[PacingPolicy] = None, clock: Callable[[], float] = time.monotonic
    ) -> None:
        super().__init__(policy)
        self._clock = clock  # wait() sleeps, so it needs a clock that runs
        self._seeded: Set[str] = set()
        self._next_at: Dict[str, float] = {}  # the next free turn
        self._block_until: Dict[str, float] = {}  # the site said to wait until then
        self._burst_until: Dict[str, float] = {}  # refusals before this are one burst
        self._passed: Dict[str, Deque[float]] = {}  # when recent requests succeeded
        self._too_fast: Dict[str, Tuple[float, float]] = {}  # (a gap that was refused, when)
        self._counting: Set[str] = set()  # sites that refused with a wait: paced evenly
        self._closed_until: Dict[str, float] = {}  # the end of the closure a site named

    def learn(self, origin: str, interval: float) -> None:
        with self._lock:
            if origin in self._seeded:
                return
            self._seeded.add(origin)
            interval = min(max(interval, 0.0), CARRY_OVER_MAX)
            self._interval[origin] = max(interval, self.policy.interval)

    def throttled(self, origin: str, retry_after: Optional[float] = None) -> float:
        policy = self.policy
        now = self._clock()
        named: float = 0.0  # the wait the site named, if it is a number of seconds
        if retry_after is not None and math.isfinite(retry_after) and retry_after > 0:
            named = retry_after
        wait: float = min(named, MAX_SITE_BLOCK)  # "inf" or an hour must not hold it for good
        with self._lock:
            self._seeded.add(origin)
            self._streak[origin] = 0
            current = self._interval.get(origin, policy.interval) or policy.floor
            last = self._burst_until.get(origin)
            if last is not None and now < last:
                return current  # the same closed window, refusing what was in flight
            if wait and now < self._closed_until.get(origin, 0.0):
                # still the closure the site named before, longer than one block:
                # hold it again, and leave the pace as it was
                self._block_until[origin] = now + wait
                self._burst_until[origin] = now + max(wait, REFUSAL_BURST)
                return current
            repeat = last is not None and now - last < REPEAT_WINDOW
            widened = current * policy.backoff_factor
            refused_gap = current
            if wait:
                was_even = origin in self._counting
                self._counting.add(origin)
                self._block_until[origin] = now + wait
                self._closed_until[origin] = now + min(named, MAX_CLOSURE)
                passed = sum(1 for at in self._passed.get(origin, ()) if now - at <= wait)
                # A wait no longer than a block is the rest of a counting window,
                # which says how many it takes; a longer one is a penalty, which
                # says nothing about pace, so the gap only steps up.
                if passed >= 3 and not repeat and named <= MAX_SITE_BLOCK:
                    allowed = wait / passed  # what the site let through before refusing
                    widened = max(allowed * LIMIT_MARGIN, current * REFUSED_STEP)
                    # Uneven gaps trip a counter at a slower average than even
                    # ones would, so only an evenly kept pace is itself to blame.
                    refused_gap = max(current, allowed) if was_even else allowed
                # Refused again soon after a block: the correction was not enough,
                # and what got through since says little (most of that time was
                # the block itself), so the gap doubles.
            widened = min(max(widened, policy.floor, policy.interval), policy.max_interval)
            self._interval[origin] = widened
            self._too_fast[origin] = (min(refused_gap, policy.max_interval), now)
            self._burst_until[origin] = now + max(wait, REFUSAL_BURST)
            return widened

    def eased(self, origin: str) -> float:
        policy = self.policy
        now = self._clock()
        with self._lock:
            self._passed.setdefault(origin, deque(maxlen=PASSED_KEPT)).append(now)
            current = self._interval.get(origin, policy.interval)
            if current <= policy.interval:
                return current
            streak = self._streak.get(origin, 0) + 1
            if streak < policy.recover_after:
                self._streak[origin] = streak
                return current
            self._streak[origin] = 0
            lowest = policy.interval
            refused = self._too_fast.get(origin)
            if refused is not None and now - refused[1] < TOO_FAST_TTL:
                lowest = max(lowest, refused[0] * TOO_FAST_MARGIN)
            narrowed = min(max(current * policy.recover_factor, lowest), policy.max_interval)
            self._interval[origin] = narrowed
            return narrowed

    def _turn_gap(self, origin: str) -> float:
        """The gap to leave after the turn being taken now."""
        mean = self.interval_for(origin)
        if mean <= 0:
            return 0.0
        with self._lock:
            even = origin in self._counting
        if not even:
            return self.gap(origin)  # the library's uneven, browsing-like gaps
        jitter = self._random.uniform(1 - EVEN_JITTER, 1 + EVEN_JITTER)
        return max(mean * jitter, self.policy.floor)

    def wait(self, origin: str, signal: Any = None) -> float:
        """Sleep until this caller's turn to send to ``origin``; seconds slept.

        The turn is reserved before sleeping, so threads waiting together are
        spread a gap apart instead of waking at once. A turn that comes up
        while the site has said to wait is taken again after the wait. A caller
        stopped while waiting gives its turn back, if no one has reserved one
        after it, so the next caller does not wait for it as well.
        """
        waited = 0.0
        while True:
            if signal is not None and signal.is_set():
                raise AbortedException("aborted while pacing")
            gap = self._turn_gap(origin)
            with self._lock:
                now = self._clock()
                turn = max(now, self._next_at.get(origin, 0.0), self._block_until.get(origin, 0.0))
                self._next_at[origin] = turn + gap
            while True:
                remaining = turn - self._clock()
                if remaining <= 0:
                    break
                if signal is not None and signal.is_set():
                    with self._lock:
                        if self._next_at.get(origin) == turn + gap:
                            self._next_at[origin] = turn
                    raise AbortedException("aborted while pacing")
                time.sleep(min(0.25, remaining))
            waited += max(0.0, turn - now)
            with self._lock:
                refused_meanwhile = self._clock() < self._block_until.get(origin, 0.0)
            if not refused_meanwhile:
                break
        self.mark(origin)
        return waited


def site_state(config: ScraperConfig, memory: Memory) -> SharedState:
    """Shared scraper state for ``config`` over ``memory``, paced by a SitePacer."""
    state = SharedState.create(config, memory=memory)
    state.pacer = SitePacer(config.pacing)
    return state


# How many connections page images may use to one host at once. The library
# allows two per site, which suits a site's pages. Images are static files,
# usually on an image host of their own: weebcentral's answered 12 a second
# over two connections, 28 over four and 40 over eight, nothing refused. Four
# is fewer than the six a browser opens to load the same pages of a chapter.
IMAGE_CONNECTIONS = 4

_image_fetch: ContextVar[bool] = ContextVar("lncrawl_image_fetch", default=False)
_image_gates: Dict[str, threading.BoundedSemaphore] = {}
_image_gates_lock = threading.Lock()


@contextmanager
def fetching_image() -> Iterator[None]:
    """Mark the fetches made inside as image downloads (see IMAGE_CONNECTIONS)."""
    token = _image_fetch.set(True)
    try:
        yield
    finally:
        _image_fetch.reset(token)


def is_image_fetch() -> bool:
    return _image_fetch.get()


def _image_gate(key: str) -> threading.BoundedSemaphore:
    """The connections images may use to the site ``key``, for the whole process."""
    with _image_gates_lock:
        gate = _image_gates.get(key)
        if gate is None:
            gate = _image_gates[key] = threading.BoundedSemaphore(IMAGE_CONNECTIONS)
        return gate


def _install_pacing(scraper: Any) -> None:
    """Make ``scraper`` wait for its turns this program's way, with the time limit stopped.

    The library holds one of a couple of slots per address while it waits out
    the site's gap, and a dozen download threads queue for them. That queue is
    this program's own doing, not the site being slow, and counting it made
    chapters and novel details time out while nothing had been asked yet.

    An image download takes one of the site's image connections instead of
    the library's two, and its turn on the site's clock the same way.
    """
    paced = getattr(scraper, "_paced", None)
    if paced is None:
        return

    @contextmanager
    def paused(key: str, abort: Any) -> Iterator[None]:
        pacer = getattr(scraper, "pacer", None)
        if is_image_fetch() and pacer is not None:
            gate = _image_gate(key)
            with waiting_turn():
                while not gate.acquire(timeout=0.25):
                    scraper._check_signal(abort)
                try:
                    pacer.wait(key, abort)
                except BaseException:
                    gate.release()
                    raise
            try:
                yield
            finally:
                gate.release()
            return
        with ExitStack() as stack:
            with waiting_turn():
                stack.enter_context(paced(key, abort))
            yield

    scraper._paced = paused


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
            state = site_state(scraper_config(rate), memory())
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
    _install_pacing(scraper)
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


def profile_snapshot() -> Dict[str, dict]:
    """Everything this process knows about sites, as plain data."""
    mem = memory()
    with mem._lock:
        return {key: asdict(profile) for key, profile in mem._profiles.items()}


def merge_profiles(profiles: Dict[str, dict]) -> None:
    """Keep what a download worker learned about its sites (see scheduler/workers.py).

    A site is downloaded by one process at a time, so the worker's profile of it
    is the newer one and replaces this process's.
    """
    if not profiles:
        return
    mem = memory()
    known = set(OriginProfile.__dataclass_fields__)
    with mem._lock:
        for key, fields in profiles.items():
            mem._profiles[key] = OriginProfile(**{k: v for k, v in fields.items() if k in known})
    mem.touch()


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
