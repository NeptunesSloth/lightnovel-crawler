# Contributing

Thanks for contributing! The most common contributions are:

- **Source crawlers** — adding or fixing support for a novel site
- **Bug fixes** — fixing crashes, wrong output, or server issues
- **Features** — new capabilities in the CLI or web server

---

## Dev setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.9+.

```bash
git clone https://github.com/lncrawl/lightnovel-crawler.git
cd lightnovel-crawler
make install   # sets up venv and installs all dependencies
```

Start the server with auto-reload while you work:

```bash
make dev
```

Or run the CLI directly:

```bash
uv run python -m lncrawl crawl "https://example.com/novel/url" --first 3 -f epub
```

---

## Code style

The project uses **ruff** (formatting + linting) and **pyright** (type checking).

```bash
make lint       # check — runs pyright, ruff format --check, ruff check
make lint-fix   # auto-fix ruff issues and reformat
```

Rules to keep in mind:

- Line length: 100, double quotes, target Python 3.9
- `lncrawl/cloudscraper/` is a vendored fork — patch rather than refactor

Run `make lint` before opening a PR and fix any errors it reports.

---

## Adding a source crawler

This is the most common contribution. Each source is a single Python file.

### 1. Find the right directory

Sources are organized by language and first letter of the domain:

```text
sources/
  en/a/   ← English sites starting with "a"
  zh/     ← Chinese
  multi/  ← multilingual
  ...
```

Create your file at `sources/<lang>/<letter>/sitename.py`.

### 2. Pick a base class

Check `lncrawl/templates/` first — if the site uses a common CMS (WordPress/Madara, NovelFull, etc.) there may already be a template that gives you the crawling logic for free:

```python
from lncrawl.templates.madara import MadaraTemplate

class MySiteCrawler(MadaraTemplate):
    base_url = ["https://mysite.com/"]
```

For sites without a matching template, subclass `Crawler` directly:

```python
from lncrawl.core.crawler import Crawler

class MySiteCrawler(Crawler):
    base_url = ["https://mysite.com/"]
    language = "en"
    has_mtl = False
    has_manga = False
    can_search = False

    def read_novel_info(self) -> None:
        ...

    def read_chapters(self) -> None:
        ...

    def read_chapter_content(self, chapter) -> None:
        ...
```

### 3. Validate

```bash
make check-sources   # validates all source files including yours
```

Fix any errors reported, then do a quick manual crawl:

```bash
uv run python -m lncrawl crawl "https://mysite.com/some-novel" --first 3 -f epub
```

### 4. Open a PR

One source per PR. Include the site URL in the PR title, e.g. `Add mysite.com`.

---

## Opening a PR

- **One logical change per PR.** If you are fixing a bug and adding a source, open two PRs.
- Run `make lint` and fix all errors before pushing.
- For source PRs: include a novel URL you tested against in the PR description.
- For bug fixes: describe what was wrong and how you verified the fix.

### CI on forks

Lint and build workflows run on forks automatically. See [FORKING.md](FORKING.md) for details on how CI works and how to download build artifacts from your fork.

---

## Questions?

Open a [Discussion](https://github.com/lncrawl/lightnovel-crawler/discussions) rather than an issue if you are unsure about something or want feedback before starting.
