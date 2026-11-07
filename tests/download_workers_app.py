"""A real app downloading from two local sites, for test_download_workers.

Run in its own process (the app context is one per process), with the data
directory, worker count, chapters per site and mode on the command line. Prints
one JSON line with the outcome.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import time


def serve(chapters: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/book/example":
                body = '<h1>Novel</h1><span itemprop="author">A</span>'
            elif path == "/book/example/chapters":
                body = (
                    '<ul class="chapter-list">'
                    + "".join(
                        f'<li><a title="Chapter {i}" href="/chapter-{i}">c</a></li>'
                        for i in range(1, chapters + 1)
                    )
                    + "</ul>"
                )
            elif path.startswith("/chapter-"):
                serial = path.rsplit("-", 1)[1]
                time.sleep(0.05)
                body = (
                    "<div id='content'><div class='chapter-text'>"
                    f"<p>Story text for chapter {serial}.</p></div></div>"
                )
            else:
                self.send_response(404)
                self.end_headers()
                return
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def saved_chapters() -> int:
    """Chapters downloaded with their text, across the whole library."""
    import sqlmodel as sq

    from lncrawl.context import ctx
    from lncrawl.dao import Chapter

    with ctx.db.session() as sess:
        ids = sess.exec(sq.select(Chapter.id).where(sq.col(Chapter.is_done).is_(True))).all()
    return sum(int(chapter.is_available) for chapter in ctx.chapters.get_many(list(ids)))


def main() -> None:
    data_dir, workers, chapters, mode = (
        Path(sys.argv[1]),
        int(sys.argv[2]),
        int(sys.argv[3]),
        sys.argv[4],
    )
    os.environ["LNCRAWL_DATA_PATH"] = str(data_dir)
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"
    servers = [serve(chapters), serve(chapters)]
    # two hosts, so two sites
    origins = [
        f"http://127.0.0.1:{servers[0].server_port}/",
        f"http://localhost:{servers[1].server_port}/",
    ]
    source_dir = data_dir / "sources/en/f"
    source_dir.mkdir(parents=True, exist_ok=True)
    (source_dir / "fixture.py").write_text(
        "from sources.en.n.novelfire import NovelFireCrawler\n"
        + "".join(
            f"class Fixture{k}(NovelFireCrawler):\n    base_url = [{origin!r}]\n"
            for k, origin in enumerate(origins)
        )
    )

    from lncrawl.context import ctx

    ctx.setup(sync_remote_index=False)
    ctx.config.crawler.worker_processes = workers
    ctx.config.crawler.can_use_browser = False
    user = ctx.users.get_admin()

    import logging

    from_workers = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if record.processName != "MainProcess":
                from_workers.append(record.processName)

    logging.getLogger().addHandler(Collect())
    logging.getLogger().setLevel(logging.INFO)

    roots = []
    if mode in ("export", "export-cancel"):
        # a bulk download: one unattended library-only export per site
        roots = ctx.jobs.bulk_download(user, [origin + "book/example" for origin in origins])
    else:
        for origin in origins:
            novel = ctx.crawler.fetch_novel(user.id, origin + "book/example")
            ids = ctx.chapters.list_ids(novel_id=novel.id)
            roots.append(ctx.jobs.fetch_many_chapters(user, *ids, novel_title=novel.title))

    ctx.scheduler.start()
    started = time.monotonic()
    workers_seen = 0
    killed = False
    while time.monotonic() - started < 120:
        jobs = [ctx.jobs.get(root.id) for root in roots]
        if all(job.is_done for job in jobs):
            break
        alive = [w for w in ctx.download_workers._workers if w.process.is_alive()]
        workers_seen = max(workers_seen, len(alive))
        if mode == "export-cancel" and not killed and alive and saved_chapters() > 2:
            for root in roots:
                ctx.jobs.cancel(root.id)
                ctx.scheduler.stop_job(root.id)
            killed = True
        if mode == "kill" and not killed and alive and any(job.done > 2 for job in jobs):
            for worker in alive:
                worker.process.kill()
            killed = True
        time.sleep(0.1)

    ctx.scheduler.stop()  # workers report back and stop before anything is measured
    ctx.download_workers.close()

    from lncrawl.core import scraping

    hosts = [origin.split("//", 1)[1].rstrip("/") for origin in origins]
    profiles = scraping.profile_snapshot()
    print(
        json.dumps(
            {
                "statuses": [ctx.jobs.get(root.id).status.name for root in roots],
                "available": saved_chapters(),
                "workers_seen": workers_seen,
                "killed": killed,
                "logs_from_workers": len(from_workers),
                "profiles_kept": [
                    host for host in hosts if profiles.get(host, {}).get("successes")
                ],
            }
        )
    )
    ctx.destroy()


if __name__ == "__main__":
    main()
