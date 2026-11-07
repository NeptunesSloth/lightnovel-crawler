from __future__ import annotations

from concurrent.futures import Future, as_completed
import logging
from typing import Dict, List, Optional, Set

from rich import print
import typer

from ...context import ctx
from ...enums import OutputFormat
from ...services.crawl_engine import crawl_items
from .app import app

logger = logging.getLogger(__name__)


@app.command(help="Crawl from novel page URL.")
def crawl(
    non_interactive: bool = typer.Option(
        False,
        "--noin",
        help="Disable interactive mode",
    ),
    range_all: Optional[bool] = typer.Option(
        None, "--all", is_flag=True, help="Download all chapters"
    ),
    range_first: Optional[int] = typer.Option(
        None, "--first", min=1, metavar="N", help="Download first few chapters"
    ),
    range_last: Optional[int] = typer.Option(
        None,
        "--last",
        min=1,
        metavar="N",
        help="Download latest few chapters",
    ),
    formats: List[OutputFormat] = typer.Option(
        [],
        "-f",
        "--format",
        show_choices=True,
        help="Output formats",
    ),
    username: Optional[str] = typer.Option(
        None,
        "--user",
        help="Username/Email",
    ),
    password: Optional[str] = typer.Option(
        None,
        "--pass",
        help="Password/Token",
    ),
    workers: Optional[int] = typer.Option(
        None,
        "--workers",
        min=1,
        metavar="N",
        help="Chapters to download at the same time (default: 4, or the source's own)",
    ),
    rate: Optional[float] = typer.Option(
        None,
        "--rate",
        min=0,
        metavar="REQ/S",
        help="Most requests per second to the site; 0 for no limit (default: from config)",
    ),
    url: str = typer.Argument(
        default=None,
        help="Novel details page URL.",
    ),
):
    from rich.console import Console
    from rich.panel import Panel

    from ...core.taskman import TaskManager
    from ...dao import Artifact, ChapterImage
    from ...exceptions import ServerError
    from ...utils.file_tools import format_size, open_folder
    from .prompts import (
        prompt_format_selection,
        prompt_login_details,
        prompt_open_artifact_folder,
        prompt_range_selection,
        prompt_url,
    )

    console = Console()

    # setup context
    ctx.setup()

    # ensure url
    if not url:
        if non_interactive:
            print("[red]Please enter a novel page URL[/red]")
            return
        url = prompt_url()
    if not url:
        return

    # init crawler
    try:
        crawler = ctx.sources.init_crawler(url, workers=workers, requests_per_sec=rate)
        if workers and crawler.taskman.workers != workers:
            # a source may pick its own count in initialize(); an explicit one wins
            crawler.taskman.init_executor(workers=workers)
        can_login = getattr(crawler, "can_login", False)
    except ServerError as e:
        print(f"[red]{e.format(True)}[/red]")
        return

    # get login details
    if can_login:
        if not (username and password):
            if not non_interactive:
                username, password = prompt_login_details()
        if username and password:
            crawler.login(username, password)

    # fetch novel details
    with console.status("Fetching novel details..."):
        user = ctx.users.get_admin()
        novel = ctx.crawler.fetch_novel(user.id, url, custom=crawler)
    print(
        Panel(
            "\n".join(
                filter(
                    None,
                    [
                        f"[cyan]{novel.url}[/cyan]",
                        f"[yellow][b]{novel.title}[/b][/yellow]",
                        f"[green]{novel.authors}[/green]" if novel.authors else None,
                        f"[i]{novel.volume_count} volumes, {novel.chapter_count} chapters[/i]",
                    ],
                )
            )
        )
    )

    if novel.chapter_count == 0:
        print("[red]No chapters to download[/red]")
        return

    # select chapters to download
    chapters: List[str] = []
    if not non_interactive and all(
        [
            range_all is None,
            range_first is None,
            range_last is None,
        ]
    ):
        chapters = prompt_range_selection(novel)
    else:
        chapters = ctx.chapters.list_ids(
            novel_id=novel.id, descending=bool(range_last), limit=range_last or range_first
        )
    if not chapters:
        print("[red]No chapters to download[/red]")
        return

    # select formats to bind
    if not formats:
        if non_interactive:
            formats = list(OutputFormat)
        else:
            formats = prompt_format_selection()

    # Images download alongside the chapters: each chapter's images are queued
    # as soon as it is saved. They get a pool of their own so a long image queue
    # never delays the chapters, which have a stall timeout of their own.
    image_tasks = TaskManager(workers=crawler.taskman.workers, signal=crawler.scraper.signal)
    image_futures: Dict[Future[ChapterImage], str] = {}
    queued_images: Set[str] = set()

    def queue_images(chapter_ids: List[str]) -> None:
        if not chapter_ids:
            return
        image_ids = ctx.images.list_ids(novel_id=novel.id, chapter_ids=chapter_ids)
        for image_id in ctx.crawler.pending_image_ids(image_ids):
            if image_id in queued_images:
                continue
            queued_images.add(image_id)
            future = image_tasks.submit_task(
                ctx.crawler.fetch_image,
                user.id,
                image_id,
                signal=crawler.scraper.signal,
                custom=crawler,
            )
            image_futures[future] = image_id

    try:
        # download chapters
        pending_chapters = ctx.crawler.pending_chapter_ids(chapters)
        downloaded_chapter_ids = list(set(chapters) - set(pending_chapters))
        queue_images(downloaded_chapter_ids)  # images an earlier run did not finish
        with crawler.taskman.progress_bar(
            total=len(pending_chapters), desc="Chapters", unit=" c"
        ) as bar:
            for outcome in crawl_items(
                pending_chapters,
                lambda chapter_id: crawler.taskman.submit_task(
                    ctx.crawler.fetch_chapter,
                    user.id,
                    chapter_id,
                    signal=crawler.scraper.signal,
                    custom=crawler,
                ),
                crawler.scraper.signal,
                max_inflight=crawler.taskman.workers,
            ):
                bar.update()
                if outcome.value is not None:
                    downloaded_chapter_ids.append(outcome.value.id)
                    queue_images([outcome.value.id])
                elif outcome.error is not None:
                    logger.warning(f"Chapter {outcome.item_id} failed: {outcome.error}")

        # wait for the chapter images still in flight
        with crawler.taskman.progress_bar(
            total=len(image_futures), desc="Images", unit=" img"
        ) as bar:
            for future in as_completed(image_futures):
                bar.update()
                if crawler.scraper.signal.is_set():
                    break
                if future.cancelled():
                    continue
                error = future.exception()
                if error is not None:
                    logger.warning(f"Image {image_futures[future]} failed: {error}")
    finally:
        image_tasks.close()

    # create artifacts
    format_set = set(formats)
    if OutputFormat.epub in format_set or (format_set & ctx.binder.depends_on_epub):
        format_set -= set([OutputFormat.epub])
        formats = [OutputFormat.epub] + list(format_set)
    else:
        formats = list(format_set)

    artifacts: Dict[OutputFormat, Artifact] = {}
    for fmt in formats:
        with console.status(f"Generating {fmt}..."):
            artifact = ctx.binder.make_artifact(
                novel.id,
                novel.title,
                format=fmt,
                user_id=user.id,
                epub=artifacts.get(OutputFormat.epub),
            )
        if artifact.is_available:
            artifacts[fmt] = artifact
            file = ctx.files.resolve(artifact.output_file)
            size = format_size(artifact.file_size or 0)
            print(f"[b]{fmt}[/b] ({size}): [cyan]{file}[/cyan]")
        else:
            print(f"[red]Failed to generate [b]{fmt.value}[/b][/red]")

    if not non_interactive and prompt_open_artifact_folder():
        cover_file = ctx.files.resolve(novel.cover_file)
        open_folder(cover_file.parent / "artifacts")
