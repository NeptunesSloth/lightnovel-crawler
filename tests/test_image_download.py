"""Images already in the format the app stores are kept byte for byte."""

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from PIL import Image

from lncrawl.core.crawler import Crawler


def encode(mode: str, fmt: str) -> bytes:
    out = BytesIO()
    Image.new(mode, (40, 30), "white" if mode != "CMYK" else (0, 0, 0, 0)).save(out, fmt)
    return out.getvalue()


class ImageDownloadTests(unittest.TestCase):
    def download(self, data: bytes) -> bytes:
        crawler = SimpleNamespace(
            scraper=SimpleNamespace(fetch=lambda *a, **k: SimpleNamespace(content=data))
        )
        crawler._image_bytes = lambda url: Crawler._image_bytes(crawler, url)
        with TemporaryDirectory() as folder:
            target = Path(folder) / "image.jpg"
            Crawler.download_image(crawler, "https://example.com/a", target)
            return target.read_bytes()

    def test_a_jpeg_is_kept_as_sent(self) -> None:
        for mode in ("RGB", "L"):
            data = encode(mode, "JPEG")
            self.assertEqual(self.download(data), data)

    def test_other_formats_become_jpeg(self) -> None:
        for mode, fmt in (("RGBA", "PNG"), ("P", "PNG"), ("RGB", "WEBP"), ("CMYK", "JPEG")):
            with self.subTest(fmt=fmt, mode=mode):
                saved = Image.open(BytesIO(self.download(encode(mode, fmt))))
                self.assertEqual(saved.format, "JPEG")
                self.assertIn(saved.mode, ("RGB", "L"))

    def test_a_cut_short_jpeg_is_not_saved(self) -> None:
        data = encode("RGB", "JPEG")
        with self.assertRaises(Exception):
            self.download(data[: len(data) // 2])

    def test_a_data_url(self) -> None:
        import base64

        data = encode("RGB", "JPEG")
        crawler = SimpleNamespace(scraper=None)
        url = "data:image/jpeg;base64," + base64.b64encode(data).decode()
        self.assertEqual(Crawler._image_bytes(crawler, url), data)


if __name__ == "__main__":
    unittest.main()
