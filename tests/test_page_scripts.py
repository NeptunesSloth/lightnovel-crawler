"""The pages the server sends are Python strings with their scripts inside.

One stray escape there (a ``\\n`` that Python turns into a real line break inside
a JavaScript string) is a syntax error, and a script that does not parse runs
none of its code: every button on the page stops working. Nothing else in the
tests loads these pages in a browser, so check that each script at least parses.
"""

import os
import re
import shutil
import subprocess
from tempfile import NamedTemporaryFile
import unittest

from lncrawl.server import app, downloads, reader, tools

_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


def inline_scripts():
    for module in (app, downloads, reader, tools):
        for name, value in vars(module).items():
            if isinstance(value, str) and "<script" in value:
                for index, script in enumerate(_INLINE_SCRIPT.findall(value)):
                    yield f"{module.__name__}.{name} script {index}", script


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class PageScriptTests(unittest.TestCase):
    def test_every_page_script_parses(self) -> None:
        found = 0
        for label, script in inline_scripts():
            found += 1
            with self.subTest(script=label):
                with NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as file:
                    file.write(script)
                try:
                    result = subprocess.run(
                        ["node", "--check", file.name], capture_output=True, text=True, timeout=60
                    )
                finally:
                    os.unlink(file.name)
                self.assertEqual(result.returncode, 0, result.stderr[-800:])
        self.assertGreaterEqual(found, 3)  # the tools, downloads and reader pages at least


if __name__ == "__main__":
    unittest.main()
