"""Chapter files are compressed one at a time (zstd 1.5.7.3 shares one context)."""

import unittest
from unittest.mock import patch

from lncrawl.utils import text_tools


class ZstdTurnTests(unittest.TestCase):
    def test_compressing_and_decompressing_take_turns(self) -> None:
        held = []

        def compress(data: bytes, level: int) -> bytes:
            held.append(text_tools._ZSTD_LOCK.locked())
            return b"x"

        def decompress(data: bytes) -> bytes:
            held.append(text_tools._ZSTD_LOCK.locked())
            return b"y"

        with (
            patch.object(text_tools.zstd, "compress", compress),
            patch.object(text_tools.zstd, "decompress", decompress),
        ):
            text_tools.text_compress(b"a")
            text_tools.text_decompress(b"b")
        self.assertEqual(held, [True, True])
        self.assertFalse(text_tools._ZSTD_LOCK.locked())

    def test_text_comes_back_as_it_went_in(self) -> None:
        plain = "Chapter one. ".encode() * 1000
        packed = text_tools.text_compress(plain)
        self.assertTrue(text_tools.is_compressed(packed))
        self.assertEqual(text_tools.text_decompress(packed), plain)


if __name__ == "__main__":
    unittest.main()
