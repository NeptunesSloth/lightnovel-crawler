import asyncio
from contextlib import asynccontextmanager
import mimetypes
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse

from ..assets.version import get_version
from ..context import ctx
from ..exceptions import ServerErrors, get_exception_handlers
from .api import router as api
from .middleware.staticfiles import CustomStaticFiles, StaticFilesGuard
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
    GZipMiddleware,
    minimum_size=1000,
)

app.add_middleware(StaticFilesGuard, prefix="/static")

# Experimental Features
if ctx.config.server.enable_browse_route:
    from .middleware.browser import BrowserNavigation

    app.add_middleware(BrowserNavigation, prefix="/browse")


# Add APIs
app.include_router(api, prefix="/api")

# Standalone power-user tools page (kept outside the web build dir so the
# web-sync workflow never overwrites it). Registered before the SPA catch-all.
app.include_router(tools_router)

# Mount static files
app.mount("/static", CustomStaticFiles(), name="static")


# Lightweight liveness probe — no auth, used by Docker healthcheck
@app.get("/health", include_in_schema=False)
async def health():
    job_count = ctx.jobs.count()
    user_count = ctx.users.count()
    return {
        "status": "ok",
        "version": get_version(),
        "users": user_count,
        "jobs": job_count,
    }


# Mount frontend
@app.get("/{fallback:path}", include_in_schema=False)
async def serve_web(fallback: str):
    target_file = web_dir.joinpath(fallback)
    if not target_file.is_relative_to(web_dir):
        raise ServerErrors.not_found
    loop = asyncio.get_event_loop()
    if not await loop.run_in_executor(None, target_file.is_file):
        target_file = web_dir / "index.html"
    mime_type, _ = mimetypes.guess_type(target_file)
    if not mime_type:
        mime_type = "application/octet-stream"
    if mime_type == "text/javascript":
        mime_type = "application/javascript"
    if target_file.name in {"index.html", "sw.js", "registerSW.js"}:
        no_cache = {"Cache-Control": "no-cache, no-store, must-revalidate"}
        if target_file.name == "index.html":
            html = await loop.run_in_executor(None, lambda: target_file.read_text(encoding="utf-8"))
            if "lncrawl-tools-launcher" not in html and "</body>" in html:
                html = html.replace("</body>", _TOOLS_LAUNCHER + "</body>", 1)
            return HTMLResponse(content=html, headers=no_cache)
        return FileResponse(target_file, media_type=mime_type, headers=no_cache)
    return FileResponse(target_file, media_type=mime_type)
