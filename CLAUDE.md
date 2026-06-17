# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Development Commands

Toolchain: [uv](https://docs.astral.sh/uv/), Python ≥ 3.9. The [Makefile](Makefile) wraps all common tasks.

```bash
make install       # setup + uv sync --all-extras --all-groups (default)
make sync          # uv sync only
make upgrade       # uv sync --upgrade

make start         # python -m lncrawl -ll server
make dev           # server with auto-reload (--watch)
make lint          # pyright lncrawl + ruff format --check + ruff check
make lint-fix      # ruff check --fix + ruff format

make index-gen     # regenerate source search index
make check-sources # validate source crawlers

make build-wheel   # python -m build -w
make build-exe     # PyInstaller (onedir on Windows, onefile on Mac/Linux)
make build-installer  # Inno Setup → dist/lncrawl.exe (Windows only)

make add-dep <pkg> / add-dev <pkg> / rm-dep <pkg> / rm-dev <pkg>
make docker-build / docker-up / docker-down / docker-logs
```

Run without make: `uv run python -m lncrawl [args]`.

No automated test suite — `test.py` is a scratchpad. Validate with lint, then:

```bash
uv run python -m lncrawl crawl "https://site.com/novel/url" --first 3 -f epub
```

## Architecture

Two things in one package: a **CLI scraper** that produces e-books, and a **FastAPI server + web UI** that turns the same engine into a multi-user job/library service. Both share a single in-process `AppContext` (`ctx`) singleton — concurrency is threads + asyncio inside one Python process.

### AppContext (`ctx`)

[lncrawl/context.py](lncrawl/context.py) exports `ctx`. Every service is a `@cached_property` — constructed on first access, never before. **Always use `ctx.<service>`; never instantiate service classes directly.** Lazy imports inside `ctx` properties are intentional — keeps CLI startup fast and avoids pulling in the FastAPI/DB stack for `lncrawl crawl`.

`ctx.setup()` boots logger → config → DB (Alembic migrations) → admin user → sources. `ctx.destroy()` closes all open services.

Services: `config`, `logger`, `db`, `mail`, `http`, `files`, `sources`, `users`, `novels`, `tags`, `secrets`, `volumes`, `chapters`, `images`, `artifacts`, `jobs`, `history`, `libraries`, `feedback`, `announcements`, `activity`, `recommendations`, `translator`, `crawler`, `binder`, `lsp`, `scheduler`, `job_notifier`, `tier`, `admin`, `github`.

Notable ones:

- **`ctx.tier`** — `AccessManager` ([services/access.py](lncrawl/services/access.py)). All quota and permission checks (job priority, enabled formats, translation access, search limits) go through `ctx.tier.<method>(user)`.
- **`ctx.http`** — `FetchService` ([services/fetch.py](lncrawl/services/fetch.py)). Shared `Scraper` instance behind an `EventLock`. Use `ctx.http.session(signal)` as a context manager to get the scraper. All HTTP outside of crawler sessions (translator backends, favicon, GitHub) goes through this.
- **`ctx.job_notifier`** — `JobNotificationService` ([services/notifications/](lncrawl/services/notifications/)). Dispatches email on job state changes via a background `TaskManager`. Triggered from handler helpers (`_set_running`, `_set_success`, etc.).

### Entry Points

- [lncrawl/__main__.py](lncrawl/__main__.py) → [lncrawl/app.py](lncrawl/app.py): Typer CLI (`version`, `config`, `sources`, `crawl`, `search`, `server`, `app`). As a frozen PyInstaller exe, bypasses CLI and calls `webview.start()` directly.
- [lncrawl/server/app.py](lncrawl/server/app.py): FastAPI app. `lifespan` calls `ctx.setup()` then `ctx.scheduler.start()`. API at `/api`, SPA at `/`, OpenAPI at `/docs`.
- [lncrawl/server/webview.py](lncrawl/server/webview.py): Desktop launcher — Chrome/Edge app-mode, falls back to system browser + tkinter status window.

### Job Scheduler

[lncrawl/services/scheduler/](lncrawl/services/scheduler/) — `JobScheduler` spawns worker threads. `JobRunner` picks and dispatches jobs; `Scrubber` handles cleanup.

Job execution is handled by per-type handler classes in [scheduler/handlers/](lncrawl/services/scheduler/handlers/). Each handler implements `BaseHandler` or `BatchHandler` with `can_activate(job)` and `run()`. `run_job()` in `handlers/__init__.py` walks the `_HANDLER_REGISTRY` list and calls the first matching handler. To add a new job type: create the handler module, add it to the registry.

`JobRunner.run()` holds `_lock` (an `EventLock`) for the entire find-and-claim phase — both `_pending()` calls and the `_queue` write — to prevent two workers from picking the same job. The actual `run_job()` call happens outside the lock.

Job status is polled via REST; there is no dedicated job WebSocket. The only WebSocket (`/api/lsp`) is a Language Server Protocol proxy.

### Source Crawlers

Sources live in [sources/](sources/) grouped by language (`en/<letter>/`, `zh/`, `ja/`, `multi/`). User sources load from `ctx.config.crawler.user_sources`.

Base classes: `Crawler` (abstract, [lncrawl/core/crawler.py](lncrawl/core/crawler.py)) and `Scraper` (HTTP/BS4/Cloudflare, [lncrawl/core/scraper.py](lncrawl/core/scraper.py)). Concurrency via `TaskManager` ([lncrawl/core/taskman.py](lncrawl/core/taskman.py)).

Preferred starting point: copy from [sources/_examples/](sources/_examples/) — `_01_general_soup.py` for the common case; `_02` for searchable; `_05`/`_07` for explicit volumes; `_09`–`_17` for JS-rendered sites. Full guide: [.github/docs/CREATING_CRAWLERS.md](.github/docs/CREATING_CRAWLERS.md). Scaffold with `make add-source`.

### Persistence

ORM: SQLModel/SQLAlchemy. Models in [lncrawl/dao/](lncrawl/dao/). DB URL from `ctx.config.db.url` — defaults to SQLite, supports PostgreSQL via `DATABASE_URL`. Migrations via Alembic in [lncrawl/migrations/](lncrawl/migrations/), run automatically on startup. Enums in [lncrawl/enums.py](lncrawl/enums.py), re-exported from `dao/__init__.py`.

### Output & Translation

**Binder** ([services/binder/](lncrawl/services/binder/)): EPUB is native; other formats (MOBI, PDF, AZW3, DOCX, FB2, …) via Calibre's `ebook-convert`; `json.py` and `text.py` are dependency-free.

**Translator** ([services/translators/](lncrawl/services/translators/)): wraps Bing, Google (3 variants), Lingva, Baidu with automatic failover. All backends use `ctx.http.session(signal)` for HTTP — they share the single `FetchService` scraper. Backend base class is `_base.py`.

### Server API

Routers in [lncrawl/server/api/](lncrawl/server/api/), aggregated in `__init__.py`. Auth via `Security(ensure_user)` / `Security(ensure_admin)` / `Security(ensure_local)` from [server/security.py](lncrawl/server/security.py). Pydantic request/response models in [server/models/](lncrawl/server/models/), distinct from the SQLModel DAO models.

### Configuration

[lncrawl/config.py](lncrawl/config.py): typed config. Data dir: `LNCRAWL_DATA_PATH` env var, else `typer.get_app_dir("LNCrawl")` (= `%APPDATA%\LNCrawl` on Windows). Properties marked `Sensitive` are redacted in the admin API.

## Conventions

- **`ruff`** ([pyproject.toml](pyproject.toml)): line-length 100, double quotes, target py39. Excludes `lncrawl/cloudscraper` (vendored fork), `lncrawl-web`, `res`, `logs`.
- **f-strings**: always use f-strings for string interpolation — never `%`-formatting or `.format()`.
- **Type annotations**: always add type annotations to function signatures and variable declarations.
- **README.md** source list and CLI help blocks are auto-generated — don't hand-edit those regions.
- **`lncrawl/cloudscraper/`** is a vendored fork — patch rather than refactor.

## Windows Packaging

PyInstaller via `setup_pyi.py`: `--onedir` on Windows (`dist/lncrawl/`), `--onefile` on Mac/Linux (`dist/lncrawl`). `make build-installer` runs Inno Setup 6 → `dist/lncrawl.exe`. The **AppId GUID** in [installer/installer.iss](installer/installer.iss) must never change — Inno Setup uses it to identify upgrades. Default install is per-user (`PrivilegesRequired=lowest`, no UAC).
