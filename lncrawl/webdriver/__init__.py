import logging
import os
import threading
from typing import List, Optional

from ..context import ctx

logger = logging.getLogger(__name__)

_warmup_lock = threading.Lock()
_warmed_up = False


# CDP domains nodriver imports lazily at runtime (cookies → network, screenshots →
# page, challenge solving → fetch/target, etc.). Importing them explicitly during
# warmup steps on any circular-import landmine now, in one thread, instead of later
# under concurrency.
_CDP_RUNTIME_SUBMODULES = (
    "network",
    "page",
    "fetch",
    "target",
    "runtime",
    "dom",
    "input_",
    "browser",
    "storage",
    "emulation",
    "security",
    "log",
)


def _force_import_cdp() -> None:
    """Import ``nodriver`` and every runtime CDP submodule, completely.

    ``nodriver/cdp/__init__.py`` imports ~50 inter-dependent submodules on one
    statement; those submodules import each other, so the package is only safe to
    use once that statement has run to completion. If a *prior* attempt (e.g. a
    concurrent first import, or a frozen-build import-order quirk) left the package
    cached half-initialized, every later ``from nodriver.cdp import network`` keeps
    re-raising::

        ImportError: cannot import name 'network' from partially initialized
        module 'nodriver.cdp' (most likely due to a circular import)

    So on failure we purge the whole ``nodriver`` namespace from ``sys.modules``
    and re-import cleanly. This is only ever called before any browser/Tab object
    exists (see callers), so dropping and rebuilding the modules is safe.
    """
    import importlib
    import sys

    last_err: Optional[BaseException] = None
    for _ in range(2):
        try:
            import nodriver  # noqa: F401  (its __init__ pulls in nodriver.cdp)
            import nodriver.cdp  # noqa: F401

            for name in _CDP_RUNTIME_SUBMODULES:
                importlib.import_module(f"nodriver.cdp.{name}")
            return
        except ImportError as e:
            last_err = e
            logger.warning("nodriver.cdp import incomplete (%s); purging and retrying", e)
            for mod in [
                m for m in list(sys.modules) if m == "nodriver" or m.startswith("nodriver.")
            ]:
                sys.modules.pop(mod, None)
    if last_err is not None:
        raise last_err


def warmup_nodriver() -> None:
    """Force ``nodriver`` + all CDP submodules to import once, single-threaded.

    Call this from a single-threaded point (scheduler startup) before any worker
    can touch a browser, and again — as a no-op safety net — when the first
    browser is created. After it returns, every ``import nodriver.cdp.*`` anywhere
    is a cached no-op, so the half-initialized-package crash can't occur.
    """
    global _warmed_up
    if _warmed_up:
        return
    with _warmup_lock:
        if _warmed_up:
            return
        _force_import_cdp()
        _warmed_up = True


def create_new(
    extra_args: Optional[List[str]] = None,
    timeout: Optional[float] = None,
    user_data_dir: Optional[str] = None,
    headless: bool = False,
    **kwargs,
):
    """Create a new nodriver browser instance."""
    # Safety net: finish importing nodriver/cdp before this browser is built, in
    # case the single-threaded startup warmup didn't run (e.g. CLI path).
    warmup_nodriver()

    if not user_data_dir:
        user_data_dir = str(ctx.config.app.app_dir / "webdriver")
        os.makedirs(user_data_dir, exist_ok=True)

    from .local import create_local

    return create_local(
        extra_args=extra_args,
        timeout=timeout,
        user_data_dir=user_data_dir,
        headless=headless,
    )
