from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from lncrawl.exceptions import ServerError
from lncrawl.services.jobs import service
from lncrawl.services.jobs.service import JobService


class OneNovelDownloadTests(unittest.TestCase):
    """A pasted novel link downloads that novel, or says why it cannot."""

    def setUp(self) -> None:
        def get_source(site: str) -> object:
            if site != "novelfire.net":
                raise KeyError(site)
            return object()

        fake = SimpleNamespace(
            sources=SimpleNamespace(get_domain=lambda url: url.split("/")[2], get_source=get_source)
        )
        patcher = patch.object(service, "ctx", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.jobs = JobService()
        self.jobs.export_source = MagicMock(return_value="job")  # type: ignore[method-assign]

    def test_a_novel_link_is_downloaded_alone(self) -> None:
        url = "https://novelfire.net/book/the-novel"
        self.assertEqual(self.jobs.bulk_download(MagicMock(), [url]), ["job"])
        kwargs = self.jobs.export_source.call_args.kwargs
        self.assertEqual(kwargs["urls"], [url])
        self.assertTrue(kwargs["library_only"])

    def test_a_site_the_app_cannot_read_is_named(self) -> None:
        with self.assertRaises(ServerError) as caught:
            self.jobs.bulk_download(MagicMock(), ["https://unknown.example/book/x"])
        self.assertIn("unknown.example", caught.exception.extra or "")


if __name__ == "__main__":
    unittest.main()
