from contextlib import contextmanager
import hashlib
import logging
import os
from pathlib import Path
import shutil
from threading import Event
import time
from typing import Optional

from scraper import Scraper

from ..assets.images import favicon_icon
from ..context import ctx
from ..utils.event_lock import EventLock
from ..utils.url_tools import extract_base

logger = logging.getLogger(__name__)


class FetchService:
    def __init__(self) -> None:
        self.lock = EventLock()
        self.scraper = Scraper()

    def close(self):
        self.scraper.close()

    @contextmanager
    def session(self, signal: Optional[Event] = None):
        original_signal = None
        try:
            with self.lock.using(signal):
                if signal:
                    original_signal = self.scraper.signal
                    self.scraper.signal = signal
                yield self.scraper
        finally:
            if original_signal:
                self.scraper.signal = original_signal

    def get(
        self,
        url: str,
        signal: Optional[Event] = None,
    ) -> bytes:
        with self.session(signal) as sess:
            resp = sess.get(url)
            resp.raise_for_status()
            return resp.content

    def download(
        self,
        url: str,
        file: Path,
        signal: Optional[Event] = None,
    ) -> None:
        content = self.get(url, signal)
        file.parent.mkdir(parents=True, exist_ok=True)
        tid = time.thread_time_ns() % 1000
        tmp = file.with_suffix(f"{file.suffix}.tmp{tid}")
        try:
            tmp.write_bytes(content)
            os.replace(tmp, file)
        finally:
            tmp.unlink(missing_ok=True)
        logger.debug(f"Downloaded: {file}")

    def favicon(
        self,
        url: str,
        signal: Optional[Event] = None,
    ) -> Path:
        favicon_url = f"{extract_base(url)}favicon.ico"

        filename = hashlib.md5(favicon_url.encode()).hexdigest()
        out_file = ctx.files.resolve(f"images/{filename}.ico")
        if out_file.is_file():
            return out_file

        try:
            self.download(favicon_url, out_file, signal)
        except Exception:
            logger.info(f"Failed to download favicon: {url}")
            shutil.copy(favicon_icon(), out_file)

        return out_file
