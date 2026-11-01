"""Run the Reader's browser tests against a real server and a seeded library.

    uv run python tests/e2e/run_reader_e2e.py

Needs Node.js with Playwright and its Chromium (`npm i playwright` and
`npx playwright install chromium`). A throwaway data folder is made, a small
library is written into it, the app's server is started on a free port, and
tests/e2e/reader.e2e.js drives the Reader in Chromium. Anything that would need
the internet (searching sources, re-reading a novel from its site) is answered
by the test itself, so the run is the same offline or on CI.
"""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from typing import Dict, List
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _seed(data_dir: Path) -> Dict[str, str]:
    """Write the test library; returns novel ids by title."""
    script = r"""
import json, sys
from lncrawl.context import ctx
ctx.config.load(); ctx.db.bootstrap(False)
from lncrawl.dao import Chapter, Novel
from lncrawl.utils.time_utils import current_timestamp
now = current_timestamp()
rows = [
    ("Solo Leveling: Ragnarok", "a.com", "Daul", "Sung Suho fights monsters.", ["Action", "Manhwa"], {"status": "ongoing"}, True),
    ("Na Honjaman Level-Up", "b.com", "Chugong", "The hunter story.", ["Action"], {"status": "completed", "alt_titles": ["Solo Leveling"]}, False),
    ("The Beginning After the End", "a.com", "TurtleMe", "A king reborn.", ["Fantasy", "BL"], {"status": "ongoing"}, False),
    ("Beginning After The End", "c.com", "TurtleMe", "Reincarnated king.", ["Fantasy", "Boys' Love"], {"status": "hiatus"}, False),
    ("Omniscient Reader", "c.com", "Sing Shong", "The end of the world.", ["Sci-fi"], {"status": "completed"}, False),
    ("Reading Now", "d.com", "Writer", "A short read.", ["Science Fiction"], {}, False),
]
ids = {}
for i in range(70):
    rows.append((f"Filler {i:02}", "f.com", "Filler", "Filler.", ["Drama"], {"status": "ongoing"}, False))
for i, (title, dom, author, syn, tags, extra, manga) in enumerate(rows):
    with ctx.db.session() as s:
        n = Novel(url=f"https://{dom}/n/{i}", title=title, domain=dom, authors=author, synopsis=syn,
                  tags=tags, extra=extra, manga=manga, chapter_count=5 if title == "Reading Now" else 1,
                  created_at=1000 + i)
        s.add(n); s.commit(); s.refresh(n); ids[title] = n.id
with ctx.db.session() as s:
    for serial in range(1, 6):
        c = Chapter(novel_id=ids["Reading Now"], serial=serial, title=f"Chapter {serial}",
                    url=f"https://d.com/r/{serial}", is_done=True,
                    created_at=now - (6 - serial) * 3600 * 1000)
        s.add(c); s.commit(); s.refresh(c)
        ctx.files.save_text(c.content_file, "<p>" + ("Words. " * 400) + "</p>")
print(json.dumps(ids))
"""
    env = dict(os.environ, LNCRAWL_DATA_PATH=str(data_dir))
    out = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def _login(base: str) -> str:
    body = json.dumps({"email": "admin", "password": "admin"}).encode()
    req = Request(f"{base}/api/auth/login", data=body, headers={"content-type": "application/json"})
    with urlopen(req, timeout=10) as res:
        return str(json.loads(res.read())["token"])


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="reader-e2e-") as tmp:
        data_dir = Path(tmp) / "data"
        ids = _seed(data_dir)
        port = _free_port()
        base = f"http://127.0.0.1:{port}"
        env = dict(os.environ, LNCRAWL_DATA_PATH=str(data_dir))
        log = open(Path(tmp) / "server.log", "w")
        cmd: List[str] = [sys.executable, "-m", "lncrawl", "server", "--host", "127.0.0.1"]
        server = subprocess.Popen(
            cmd + ["--port", str(port)], cwd=ROOT, env=env, stdout=log, stderr=log
        )
        try:
            token = ""
            for _ in range(120):
                try:
                    token = _login(base)
                    break
                except Exception:
                    time.sleep(1)
            if not token:
                log.flush()
                print((Path(tmp) / "server.log").read_text()[-4000:])
                print("The server did not come up")
                return 1
            run_env = dict(os.environ, BASE=base, TOKEN=token, IDS=json.dumps(ids))
            return subprocess.call(["node", str(HERE / "reader.e2e.js")], cwd=ROOT, env=run_env)
        finally:
            server.terminate()
            try:
                server.wait(timeout=20)
            except subprocess.TimeoutExpired:
                server.kill()
            log.close()


if __name__ == "__main__":
    sys.exit(main())
