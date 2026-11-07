import functools
import logging
from pathlib import Path
import threading
from typing import TYPE_CHECKING, Any, Optional, Union

logger = logging.getLogger(__name__)

_MISSING = object()


class _SynchronizedCachedProperty(functools.cached_property):
    """``functools.cached_property`` that builds each service once, whatever the threads do.

    Python 3.12 dropped the lock the standard one held while building a value, so
    two threads asking for a service at once (the scheduler's runners, say)
    each built their own: two pools of download workers, each spawning its own
    processes, enforcing its own limits, and one never closed. One lock for all
    of the context, reentrant because building a service often asks for another.
    """

    _lock = threading.RLock()

    def __get__(self, instance: Any, owner: Any = None) -> Any:
        if instance is None:
            return self
        cache = instance.__dict__
        value = cache.get(self.attrname, _MISSING)
        if value is _MISSING:
            with self._lock:
                value = cache.get(self.attrname, _MISSING)
                if value is _MISSING:
                    value = self.func(instance)
                    cache[self.attrname] = value
        return value


if TYPE_CHECKING:  # type checkers keep each service's own type
    from functools import cached_property
else:
    cached_property = _SynchronizedCachedProperty


class __AppContext__:
    @cached_property
    def config(self):
        from .config import Config

        return Config()

    @cached_property
    def activity(self):
        from .services.activity import UserActivityService

        return UserActivityService()

    @cached_property
    def admin(self):
        from .services.admin import AdminService

        return AdminService()

    @cached_property
    def github(self):
        from .services.github import GitHubService

        return GitHubService()

    @cached_property
    def logger(self):
        from .services.logger import Logger

        return Logger()

    @cached_property
    def db(self):
        from .services.db import DB

        return DB()

    @cached_property
    def mail(self):
        from .services.mail import MailService

        return MailService()

    @cached_property
    def http(self):
        from .services.fetch import FetchService

        return FetchService()

    @cached_property
    def files(self):
        from .services.file import FileService

        return FileService()

    @cached_property
    def sources(self):
        from .services.sources import Sources

        return Sources()

    @cached_property
    def users(self):
        from .services.users import UserService

        return UserService()

    @cached_property
    def novels(self):
        from .services.novels import NovelService

        return NovelService()

    @cached_property
    def recommendations(self):
        from .services.recommendations import RecommendationService

        return RecommendationService()

    @cached_property
    def tags(self):
        from .services.tags import TagService

        return TagService()

    @cached_property
    def secrets(self):
        from .services.secrets import SecretService

        return SecretService()

    @cached_property
    def volumes(self):
        from .services.volumes import VolumeService

        return VolumeService()

    @cached_property
    def chapters(self):
        from .services.chapters import ChapterService

        return ChapterService()

    @cached_property
    def images(self):
        from .services.images import ChapterImageService

        return ChapterImageService()

    @cached_property
    def artifacts(self):
        from .services.artifacts import ArtifactService

        return ArtifactService()

    @cached_property
    def jobs(self):
        from .services.jobs.service import JobService

        return JobService()

    @cached_property
    def history(self):
        from .services.history import ReadHistoryService

        return ReadHistoryService()

    @cached_property
    def libraries(self):
        from .services.libraries import LibraryService

        return LibraryService()

    @cached_property
    def feedback(self):
        from .services.feedback import FeedbackService

        return FeedbackService()

    @cached_property
    def announcements(self):
        from .services.announcements import AnnouncementService

        return AnnouncementService()

    @cached_property
    def translator(self):
        from .services.translators import TranslationService

        return TranslationService()

    @cached_property
    def download_workers(self):
        from .services.scheduler.workers import DownloadWorkers

        return DownloadWorkers()

    @cached_property
    def crawler(self):
        from .services.crawler import CrawlerService

        return CrawlerService()

    @cached_property
    def binder(self):
        from .services.binder import BinderService

        return BinderService()

    @cached_property
    def lsp(self):
        from .services.lsp import PythonLanguageServer

        return PythonLanguageServer()

    @cached_property
    def tier(self):
        from .services.access import AccessManager

        return AccessManager()

    @cached_property
    def scheduler(self):
        from .services.scheduler import JobScheduler

        return JobScheduler()

    @cached_property
    def job_notifier(self):
        from .services.notifications import JobNotificationService

        return JobNotificationService()

    # ------------------------------------------------------------
    # Context management
    # ------------------------------------------------------------

    def __init__(self) -> None:
        self.__ready = False

    def destroy(self):
        self.__ready = False
        if "scheduler" in self.__dict__:
            self.scheduler.stop()
        if "download_workers" in self.__dict__:
            self.download_workers.close()
        if "crawler" in self.__dict__:
            self.crawler.close()  # pooled crawlers, once no job can be using one
        if "sources" in self.__dict__:
            self.sources.close()
        if "mail" in self.__dict__:
            self.mail.close()
        if "db" in self.__dict__:
            self.db.close()
        if "lsp" in self.__dict__:
            self.lsp.stop()
        if "translator" in self.__dict__:
            self.translator.close()
        if "http" in self.__dict__:
            self.http.close()
        # Write out what the scrapers learned (pacing, solved clearances) so the
        # next run starts from it instead of re-solving. Memory also flushes on a
        # timer, so this only matters for whatever happened since the last one.
        from .core.scraping import close_shared_state

        close_shared_state()

    def setup(
        self,
        log_level: Union[int, str, None] = None,
        config_file: Optional[Path] = None,
        reset_db_on_failure: bool = False,
        sync_remote_index=True,
    ):
        if self.__ready:
            return
        self.__ready = True
        self.logger.setup(log_level)
        self.config.load(config_file)
        self.db.bootstrap(reset_db_on_failure)
        self.users.setup_admin()
        self.secrets.setup_secret()
        self.sources.load(sync_remote_index)


ctx = __AppContext__()
