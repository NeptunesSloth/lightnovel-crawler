import logging
import os
import threading
from typing import List, Optional

from ..context import ctx

logger = logging.getLogger(__name__)

_warmup_lock = threading.Lock()
_warmed_up = False


def _warmup_nodriver() -> None:
    """Import ``nodriver`` (and all its CDP submodules) once, single-threaded.

    ``nodriver/__init__.py`` eagerly pulls in ``nodriver.cdp``, which in turn
    imports ~50 inter-dependent CDP submodules on a single statement. When
    several crawler workers trigger that *first* import concurrently (e.g. a
    batch export starting many JS-rendered novels at once), one thread can
    observe the package half-initialized and crash with::

        ImportError: cannot import name 'network' from partially initialized
        module 'nodriver.cdp' (most likely due to a circular import)

    Forcing the import to complete under a lock before any worker uses the
    browser removes the race; afterwards every ``import nodriver.cdp.*`` is a
    cached no-op.
    """
    global _warmed_up
    if _warmed_up:
        return
    with _warmup_lock:
        if _warmed_up:
            return
        import nodriver  # noqa: F401  (pulls in nodriver.cdp + all submodules)
        import nodriver.cdp  # noqa: F401

        _warmed_up = True


def create_new(
    extra_args: Optional[List[str]] = None,
    timeout: Optional[float] = None,
    user_data_dir: Optional[str] = None,
    headless: bool = False,
    **kwargs,
):
    """Create a new nodriver browser instance."""
    # Eagerly finish importing nodriver/cdp before any concurrent browser use.
    _warmup_nodriver()

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
