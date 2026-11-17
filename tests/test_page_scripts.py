"""The pages the server sends are Python strings with their scripts inside.

One stray escape there (a ``\\n`` that Python turns into a real line break inside
a JavaScript string) is a syntax error, and a script that does not parse runs
none of its code: every button on the page stops working. Nothing else in the
tests loads these pages in a browser, so check that each script at least parses.
"""

import ast
import os
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import NamedTemporaryFile
import unittest

_INLINE_SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)
_SERVER = Path(__file__).resolve().parents[1] / "lncrawl" / "server"
_PAGES = ("app.py", "downloads.py", "reader.py", "tools.py")


def inline_scripts():
    """Each script in the page strings, as Python hands it to the browser.

    Read from the source, not by importing: `lncrawl.server.app` builds the
    whole server when imported and needs a data folder to exist.
    """
    for name in _PAGES:
        tree = ast.parse((_SERVER / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for index, script in enumerate(_INLINE_SCRIPT.findall(node.value)):
                    yield f"{name} line {node.lineno} script {index}", script


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
        self.assertGreaterEqual(found, 4)  # the tools, downloads and reader pages, and the launcher


if __name__ == "__main__":
    unittest.main()
