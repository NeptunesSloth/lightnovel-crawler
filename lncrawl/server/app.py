import asyncio
from contextlib import asynccontextmanager
import mimetypes
from pathlib import Path
from typing import Dict, Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

from ..assets.version import get_version
from ..context import ctx
from ..exceptions import ServerErrors, get_exception_handlers
from .api import router as api
from .caching import Blob, FileBlobCache, blob_response, text_blob
from .downloads import router as downloads_router
from .middleware.compression import SelectiveGZipMiddleware
from .middleware.staticfiles import CustomStaticFiles, StaticFilesGuard
from .reader import router as reader_router
from .tools import router as tools_router

web_dir = (Path(__file__).parent / "web").absolute()

# Floating launcher injected into the SPA so the standalone /tools page is
# reachable from the main app (the desktop window has no address bar). Injected
# at serve-time, so the web-sync workflow that overwrites web/ never affects it.
_TOOLS_LAUNCHER = """<script>(function(){
  var t = new URLSearchParams(location.search).get('authToken') || '';
  document.addEventListener('DOMContentLoaded', function(){
    if (document.getElementById('lncrawl-tools-launcher')) return;
    var a = document.createElement('a');
    a.id = 'lncrawl-tools-launcher';
    a.href = '/tools' + (t ? ('?authToken=' + encodeURIComponent(t)) : '');
    a.textContent = '\\uD83D\\uDEE0 Tools';
    a.title = 'Bulk download a whole source / power-user tools';
    a.style.cssText = 'position:fixed;right:14px;bottom:14px;z-index:99999;'
      + 'background:#3b82f6;color:#fff;padding:8px 14px;border-radius:999px;'
      + 'font:600 13px system-ui,sans-serif;text-decoration:none;'
      + 'box-shadow:0 2px 10px rgba(0,0,0,.4)';
    document.body.appendChild(a);
  });
})();</script>"""


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ctx.setup()
        # The server reports progress through the web UI, never a console bar.
        # Drawing a tqdm bar here is pointless and, in the windowed desktop build
        # whose console handle goes invalid after launch, raises
        # "OSError: [WinError 6] The handle is invalid" mid-download. Force it off.
        ctx.logger.progress_bar = False
        ctx.mail.start()
        ctx.scheduler.start()
        ctx.recommendations.warmup()
        yield
    finally:
        ctx.destroy()


app = FastAPI(
    version=get_version(),
    title="Lightnovel Crawler",
    description="Download novels from online sources and generate e-books",
    lifespan=lifespan,
    exception_handlers=get_exception_handlers(),
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    terms_of_service="https://raw.githubusercontent.com/lncrawl/lncrawl-web/refs/heads/artifacts/TERMS_OF_SERVICE.html",
    contact={
        "email": "lncrawl@pm.me",
        "name": "Lightnovel Crawler",
        "url": "https://github.com/lncrawl/lightnovel-crawler",
    },
    license_info={
        "name": "License: GPLv3",
        "url": "https://raw.githubusercontent.com/lncrawl/lightnovel-crawler/dev/LICENSE",
    },
)


# Add middleares
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(
    SelectiveGZipMiddleware,
    minimum_size=1000,
)

app.add_middleware(StaticFilesGuard, prefix="/static")

# Experimental Features
if ctx.config.server.enable_browse_route:
    from .middleware.browser import BrowserNavigation

    app.add_middleware(BrowserNavigation, prefix="/browse")


# Add APIs
app.include_router(api, prefix="/api")

# Standalone power-user tools page and in-app reader (kept outside the web build
# dir so the web-sync workflow never overwrites them). Before the SPA catch-all.
app.include_router(tools_router)
app.include_router(downloads_router)
app.include_router(reader_router)

# Mount static files
app.mount("/static", CustomStaticFiles(), name="static")


# Lightweight liveness probe — no auth, used by Docker healthcheck
# (sync: the counts are DB queries, so FastAPI runs it off the event loop)
@app.get("/health", include_in_schema=False)
def health() -> Dict[str, object]:
    job_count = ctx.jobs.count()
    user_count = ctx.users.count()
    return {
        "status": "ok",
        "version": get_version(),
        "users": user_count,
        "jobs": job_count,
    }


# Mount frontend
#
# The build under web/ is held in memory, gzipped once, and refreshed when a
# file changes on disk, so a page load neither re-reads nor re-compresses the
# ~4 MB of bundles on the event loop. Vite names everything under assets/ by
# its content hash (Home-CaOuWaxX.js), so those are cached by the browser for
# good; the entry points that must pick up a new build stay uncached.
_NO_CACHE = "no-cache, no-store, must-revalidate"
_IMMUTABLE = "public, max-age=31536000, immutable"
_web_cache = FileBlobCache()
_web_root = web_dir.resolve()


def _web_media_type(target_file: Path) -> str:
    mime_type, _ = mimetypes.guess_type(target_file)
    if not mime_type:
        return "application/octet-stream"
    if mime_type == "text/javascript":
        return "application/javascript"
    return mime_type


def _load_web_file(fallback: str) -> Tuple[Path, Optional[Blob]]:
    target_file = web_dir.joinpath(fallback).resolve()
    if not target_file.is_relative_to(_web_root):
        # "/../app.py" (sent raw, e.g. curl --path-as-is) must not leave web/
        raise ServerErrors.not_found
    if not target_file.is_file():
        target_file = web_dir / "index.html"
    if target_file.name == "index.html":
        return target_file, _load_index(target_file)
    return target_file, _web_cache.get(target_file, _web_media_type(target_file))


def _load_index(target_file: Path) -> Optional[Blob]:
    blob = _web_cache.get(target_file, "text/html")
    if blob is None:
        return None
    html = blob.body.decode("utf-8")
    if "lncrawl-tools-launcher" not in html and "</body>" in html:
        html = html.replace("</body>", _TOOLS_LAUNCHER + "</body>", 1)
    return text_blob(html, "text/html; charset=utf-8")


@app.get("/{fallback:path}", include_in_schema=False)
async def serve_web(request: Request, fallback: str) -> Response:
    loop = asyncio.get_running_loop()
    target_file, blob = await loop.run_in_executor(None, _load_web_file, fallback)
    headers: Dict[str, str] = {}
    if target_file.name in {"index.html", "sw.js", "registerSW.js"}:
        headers["Cache-Control"] = _NO_CACHE
    elif target_file.parent == _web_root / "assets":
        headers["Cache-Control"] = _IMMUTABLE
    if blob is not None:
        return blob_response(request, blob, headers)
    return FileResponse(target_file, media_type=_web_media_type(target_file), headers=headers)
