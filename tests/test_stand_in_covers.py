from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from lncrawl.core import crawler as crawler_module
from lncrawl.core.crawler import Crawler, is_stand_in_cover, stand_in_marker
from lncrawl.utils.imgen import generate_cover_image


class StandInCoverTests(unittest.TestCase):
    """A cover the app drew is replaced once the real one downloads."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.cover = self.dir / "cover.jpg"
        logger = SimpleNamespace(warn=MagicMock(), debug=MagicMock(), is_debug=False)
        patcher = patch.object(crawler_module, "ctx", SimpleNamespace(logger=logger))
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake(self, works: bool) -> SimpleNamespace:
        def download_image(url: str, output_file: Path) -> None:
            if not works:
                raise ConnectionError("403 Forbidden")
            if output_file.is_file() and output_file.stat().st_size:
                return  # what download_image does with a file already there
            Image.new("RGB", (300, 450), (10, 20, 30)).save(output_file, "JPEG")

        return SimpleNamespace(download_image=download_image, auto_generate_cover=True)

    def test_a_failed_download_draws_one_and_marks_it(self) -> None:
        Crawler.download_cover(self.fake(False), "https://x/c.jpg", self.cover)  # type: ignore[arg-type]
        self.assertTrue(self.cover.is_file())
        self.assertTrue(stand_in_marker(self.cover).is_file())
        self.assertIn("403 Forbidden", crawler_module.ctx.logger.warn.call_args[0][0])

    def test_the_real_cover_replaces_the_drawn_one(self) -> None:
        Crawler.download_cover(self.fake(False), "https://x/c.jpg", self.cover)  # type: ignore[arg-type]
        Crawler.download_cover(self.fake(True), "https://x/c.jpg", self.cover)  # type: ignore[arg-type]
        with Image.open(self.cover) as img:
            self.assertEqual(img.size, (300, 450))
        self.assertFalse(stand_in_marker(self.cover).exists())

    def test_one_drawn_before_markers_is_recognised(self) -> None:
        generate_cover_image().save(self.cover, "JPEG", optimized=True)
        self.assertTrue(is_stand_in_cover(self.cover))
        Image.new("RGB", (300, 450), (10, 20, 30)).save(self.dir / "real.jpg", "JPEG")
        self.assertFalse(is_stand_in_cover(self.dir / "real.jpg"))

    def test_a_drawn_one_is_kept_when_the_real_one_still_fails(self) -> None:
        Crawler.download_cover(self.fake(False), "https://x/c.jpg", self.cover)  # type: ignore[arg-type]
        before = self.cover.read_bytes()
        Crawler.download_cover(self.fake(False), "https://x/c.jpg", self.cover)  # type: ignore[arg-type]
        self.assertEqual(self.cover.read_bytes(), before)
        self.assertTrue(is_stand_in_cover(self.cover))


if __name__ == "__main__":
    unittest.main()
