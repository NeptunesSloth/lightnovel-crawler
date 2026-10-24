# Crawl engine and recovery

CLI downloads and source exports use `lncrawl/services/crawl_engine.py`. The engine
submits a small rolling window rather than creating a future for every chapter.
With the default bulk settings, at most four chapter or image tasks are submitted
at once. A failed chapter is recorded independently; it does not cancel the export.
After ten consecutive failures the engine stops submitting work for that novel,
drains the requests already in flight, and leaves the remaining chapters as gaps.

## Time limits

Chapter and image retrieval have a 60-second budget; metadata retrieval has a
180-second budget. Discovery searches and sitemap requests have 30-second budgets.
These budgets are scoped to the current task using a context variable, so one
chapter timing out cannot set the cancellation signal for other chapters.

The scraper receives a per-retrieval cancellation signal and socket timeouts capped
at 10 seconds to connect and 20 seconds to read, further reduced by the remaining
task budget. Browser semaphore waits and async operations share that budget.
Timed-out async work is cancelled. Existing TLS verification remains enabled.

Budgets are cooperative: they bound supported network/browser operations, not
arbitrary native code that ignores cancellation. A separate 120-second idle guard
still stops an export and records worker diagnostics if no task completes despite
these safeguards. It is a last-resort failure, not the normal retry mechanism.

## Resume and output

The scheduler does not periodically cancel active workers. On startup it requeues
jobs left running by a previous process. Cancellation retains the worker's export
lease until that worker actually exits, so Resume cannot overlap a stopping export.

Valid cached text and images are reused across crawler updates. Empty or known
NovelFire login-notice bodies are gaps. Partial and complete ebook results are
checkpointed atomically, and the selected novel URLs are retained for replay.
Chapter files use unique atomic temporary files so overlapping jobs cannot
delete or overwrite one another's temporary write.
Completed books can also be recovered from retained export ZIPs with matching
complete index entries. A ZIP stored only on another machine cannot be reused
until it is available to the app.

Explicit repair rebuilds an EPUB only after all known chapter text is present.
Bulk exports may include partial books; the ZIP index labels them incomplete.
Failed chapters remain eligible for retry and existing valid files are preserved.
Incomplete books do not enter the completed-title de-duplication set. Checkpoint
writes preserve other novels' export history, and a chapter-limited sample cannot
satisfy a later full export. Corrupt compressed/encoded chapter files are repairable
gaps; completed reader jobs do not prevent creating a new fetch job for a gap.

## Validation

Run from the checkout:

```sh
uv run --frozen python -m unittest discover -s tests -v
```

The HTTP integration tests run a local server with NovelFire-style discovery,
metadata, paginated chapter lists, cover images, nested chapter text, login-notice
responses, and slow requests. They use the actual scraper, NovelFire parser,
threaded engine, SQLite services, EPUB builder, and ZIP writer. They verify
128-chapter output, partial-export recovery, and restart/resume without repeated
HTTP requests. A separate subprocess also runs the real CLI parser and crawl
command against the fixture and checks its EPUB text. GitHub's Python-version
CI matrix runs the regression suite.

Local fixture success does not establish access to the live NovelFire website.
For a live check, select an uncached public novel, run a small EPUB export, inspect
the chapter text, and then resume it to verify reuse. If the source returns 403 or
another block, report that response; do not call the export verified. For a worker
stall, use Tools → Download stall diagnostics and retain the generated log.
