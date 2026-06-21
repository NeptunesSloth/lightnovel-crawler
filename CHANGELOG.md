# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [4.9.0] - 2026-06-21

### Added

- **IMAP inbox listener** — `MailService` gains a start/close lifecycle and an IMAP IDLE loop (via `imap-tools`, defaults to ProtonMail Bridge on `localhost:1143`). Incoming mail from an unregistered sender automatically triggers an invite through the admin referral flow; known users are left untouched. Gated behind a new `imap_enabled` config flag (off by default); outbound mail is likewise gated behind `smtp_enabled`
- **Fetch-missing / fetch-latest jobs** — two new `JobType` values, `FETCH_MISSING` (60) and `FETCH_LATEST` (61), with dedicated handlers and `POST /api/job/create/fetch-missing` / `fetch-latest` endpoints for filling chapter gaps and pulling newly released chapters. New `sync_jobtype` Alembic migration for PostgreSQL compatibility
- **Admin activity dashboard** — `GET /api/admin/activity?type=<kind>&days=<n>` backed by four focused service methods (summary, daily active users, per-type trend, top users); new response models in `server/models/activity.py`
- **Artifact download tracking** — new `ActivityType.ARTIFACT` (12) recorded when a file under `/artifacts/` is served; other static downloads remain `DOWNLOAD`
- **Browser navigation middleware** — a `/browse/*` proxy (`BrowserNavigation`) fetches remote URLs through the scraper engine and rewrites links so navigation stays within the local server, injecting a JS interceptor that routes `fetch`/XHR/anchor clicks through the same prefix. Adds a `browse_helper` utility and an `assets/scripts` package
- **Proxy toggle** — new config option to enable/disable proxy usage in the crawler

### Changed

- **Multiple proxies supported** — proxy config reworked to accept a list of proxies; `lncrawl-scraper` bumped accordingly
- **Activity worker count** — user-activity tracking now uses `runner_concurrency` as the worker count

### Fixed

- **Active-user count** — `get_admin_summary` counted activity types instead of distinct users; replaced with a `COUNT(DISTINCT user_id)` subquery, fixing PostgreSQL compatibility
- **Sources** — updated `aquareader.net` (#3035) and `lightnovelpub.org`; regenerated source index

## [4.8.0] - 2026-06-14

### Added

- **Job notifications** — `JobNotificationService` dispatches email on job state changes (pending → running → success/failure) via a background `TaskManager`; triggered from handler helpers (`_set_running`, `_set_success`, etc.)
- **Docker healthcheck** — server container now exposes a `/health` probe

### Changed

- **Job runner refactored into typed handlers** — `JobRunner` now dispatches via a `_HANDLER_REGISTRY` of `BaseHandler`/`BatchHandler` subclasses; each job type has its own module under `scheduler/handlers/`
- **Web app synced before Docker build** — `lncrawl-web` artifacts are pulled in as part of the Docker build step
- **`crawler_version` stamped on novel/chapter updates** — upserts now use a merge strategy to preserve existing data

### Fixed

- **Server hangup** — root cause of hang addressed (event lock contention / crawler resource leak)
- **Server crash** — crawler resource leak on shutdown fixed; Docker healthcheck added
- **`crawl.py`** (#3030) — regression in CLI crawl flow corrected
- **Torproxy** — re-enabled after an unintended regression

## [4.7.0] - 2026-06-13

### Added

- **Background search jobs** — novel search is now a proper background job with two new `JobType` values:
  - `SEARCH_SOURCE` — searches a single crawlable source; trigger via `POST /api/job/create/search-sources?domain=…`
  - `SEARCH_ALL_SOURCES` — fans out across every searchable source, spawning one `SEARCH_SOURCE` child per source; idempotent on retry
  - `JobRunner` handles execution: results stored in `job.extra`, matched URLs create a `NOVEL_BATCH` child job
  - New Alembic migration (`add_jobtype`) for PostgreSQL compatibility
- **PAUSED job status** — new `JobStatus.PAUSED` enum value for finer job lifecycle control
- **Per-tier search-job rate limiting** — `BASIC` users are capped at 1 concurrent search while the general active-job quota remains independent; search query length validated (2–50 chars); results sorted by match ratio
- **NovelFire search** — `SEARCH` capability added to `NovelFireCrawler` (#3009)

### Changed

- **Removed vendored `lncrawl/cloudscraper`** — the embedded Cloudflare-bypass fork (v1/v2/v3 handlers, captcha integrations, JS interpreters, 7 913-line `browsers.json`) has been removed; HTTP scraping is now delegated to the [`lncrawl-scraper`](https://github.com/dipu-bd/lncrawl-scraper) package
- **JavaScript engine replaced**: PyExecJs → quickjs → **exejs** — lighter dependency, no Node.js or external runtime required
- **Proxy support in scraper** — the `lncrawl-scraper` integration now supports proxies; `build-essentials` added to Docker base image (#3014)
- **BrowserTemplate merged into SoupTemplate** — `BrowserTemplate` is integrated directly into the soup template hierarchy rather than being a standalone class; all browser-based sources refactored accordingly
- **Job service hardening** — event locking and improved update logic in `JobService`; request timeouts in the scraper adjusted
- **Docker improvements** — faster image builds; updated `compose.yml` and server-compose files; fixed unintended root access in `server-compose`
- **truyenfull**: updated domain and search URL; `base_url` changed to a list to support multiple domains (#3010)

### Fixed

- **Security** — path-traversal / static-file exposure vulnerability fixed in `app.py` and `staticfiles.py` (#3005)
- **katreadingcafe** — chapter link validation logic corrected to filter out non-chapter URLs (#3026)
- **Race condition** — parallel search result aggregation could yield inconsistent data under concurrent writes; fixed with proper locking
- **EPUB + NovelFire** (#2993):
  - Duplicate chapter title and serial number removed from chapter body content
  - `download_chapter_body` header extraction improved (regex + text normalisation)
  - EPUB serial heading logic refactored
- **Source loading on restart** — a failure loading one source no longer aborts the full reload cycle
- **Cover download** — full error stack trace suppressed for non-critical cover fetch failures
- **PyInstaller packaging** (`setup_pyi`) — fixed a regression in frozen-binary builds

[4.9.0]: https://github.com/lncrawl/scraper/compare/v4.9.0...v4.8.0
[4.8.0]: https://github.com/lncrawl/scraper/compare/v4.8.0...v4.9.0
[4.7.0]: https://github.com/lncrawl/scraper/compare/v4.7.0...v4.8.0
