# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
