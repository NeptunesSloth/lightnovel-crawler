import atexit
import logging
from threading import Semaphore
from time import monotonic
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from nodriver import Browser

logger = logging.getLogger(__name__)

MAX_BROWSER_INSTANCES = 8

__open_browsers: List["Browser"] = []
__semaphore = Semaphore(MAX_BROWSER_INSTANCES)


def __override_stop(browser: "Browser") -> None:
    __open_browsers.append(browser)
    original_stop = browser.stop

    def override() -> None:
        if browser in __open_browsers:
            __semaphore.release()
            __open_browsers.remove(browser)
            logger.info("Destroyed browser instance")
        original_stop()

    browser.stop = override  # type: ignore[method-assign]


def acquire_queue(timeout: Optional[float] = None) -> None:
    from ..core.request_budget import current_budget

    budget = current_budget()
    if budget is not None:
        end = monotonic() + timeout if timeout is not None else budget.deadline
        while True:
            remaining = min(budget.remaining(), end - monotonic())
            if remaining <= 0:
                raise TimeoutError("Failed to acquire browser semaphore")
            if __semaphore.acquire(True, min(0.25, remaining)):
                return
    acquired = __semaphore.acquire(True, timeout)
    if not acquired:
        raise TimeoutError("Failed to acquire browser semaphore")


def release_failed_queue() -> None:
    """Return a reserved slot when browser startup did not finish."""
    __semaphore.release()


def release_queue(browser: "Browser") -> None:
    __override_stop(browser)


def check_active(browser: Optional["Browser"]) -> bool:
    if not isinstance(browser, Browser):
        return False
    return browser in __open_browsers


def cleanup_drivers() -> None:
    for browser in list(__open_browsers):
        try:
            browser.stop()
        except Exception:
            pass


atexit.register(cleanup_drivers)
