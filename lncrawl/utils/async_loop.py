import asyncio
from concurrent.futures import Future, TimeoutError
import logging
import threading
from typing import Any, Coroutine, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
_thread: threading.Thread = threading.Thread(
    target=_loop.run_forever, daemon=True, name="nodriver-loop"
)
_thread.start()


def run_async(coro: Coroutine[Any, Any, T], timeout: Optional[float] = None) -> T:
    """Submit an async coroutine to the nodriver event loop and block until done."""
    from ..core.request_budget import current_budget

    budget = current_budget()
    if budget is not None:
        try:
            remaining = budget.remaining()
        except BaseException:
            coro.close()
            raise
        timeout = min(timeout, remaining) if timeout is not None else remaining
    future: Future[T] = asyncio.run_coroutine_threadsafe(coro, _loop)
    try:
        return future.result(timeout=timeout)
    except (TimeoutError, KeyboardInterrupt):
        future.cancel()
        if budget is not None:
            budget.remaining()
        raise
