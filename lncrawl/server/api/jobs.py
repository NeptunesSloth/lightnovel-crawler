import re
from typing import Optional

from fastapi import APIRouter, Body, Path, Query, Security
from fastapi.responses import FileResponse

from ...context import ctx
from ...dao import Job, JobPriority, JobStatus, JobType, User
from ...exceptions import ServerErrors
from ..models import (
    DiscoverSourceRequest,
    ExportSourceRequest,
    FetchChaptersRequest,
    FetchImagesRequest,
    FetchLatestRequest,
    FetchMissingChaptersRequest,
    FetchNovelsRequest,
    FetchVolumesRequest,
    ListSourceRequest,
    MakeArtifactsRequest,
    NotifyConfig,
    NotifyConfigRequest,
    Paginated,
    ProxyConfig,
    ProxyConfigRequest,
    SearchSourceRequest,
    TranslateChaptersRequest,
    TranslateNovelsRequest,
    TranslateVolumesRequest,
)
from ..security import ensure_user

# The root router
router = APIRouter()


@router.get(
    "s",
    summary="Returns a list of jobs",
    dependencies=[Security(ensure_user)],
)
def list_jobs(
    offset: int = Query(default=0),
    limit: int = Query(default=20, le=100),
    type: Optional[JobType] = Query(default=None),
    user_id: Optional[str] = Query(default=None),
    status: Optional[JobStatus] = Query(default=None),
    priority: Optional[JobPriority] = Query(default=None),
    parent_job_id: Optional[str] = Query(default=None),
    is_done: Optional[bool] = Query(default=None),
) -> Paginated[Job]:
    return ctx.jobs.list(
        limit=limit,
        offset=offset,
        user_id=user_id,
        job_type=type,
        status=status,
        is_done=is_done,
        priority=priority,
        parent_job_id=parent_job_id,
    )


def _current_proxy_config() -> ProxyConfig:
    crawler = ctx.config.crawler
    return ProxyConfig(
        proxy_urls=crawler.proxy_urls,
        enable_proxy=crawler.enable_proxy,
        allow_fallback_on_proxy_miss=crawler.allow_fallback_on_proxy_miss,
    )


@router.get(
    "/proxy-config",
    summary="Get the crawler proxy / IP-rotation settings",
    dependencies=[Security(ensure_user)],
)
def get_proxy_config() -> ProxyConfig:
    return _current_proxy_config()


@router.post(
    "/proxy-config",
    summary="Update the crawler proxy / IP-rotation settings",
    dependencies=[Security(ensure_user)],
)
def set_proxy_config(
    body: ProxyConfigRequest = Body(),
) -> ProxyConfig:
    # Accept comma- or newline-separated input from the UI and normalize to the
    # comma-separated form the crawler expects. New exports pick this up at
    # init_crawler time, so the change applies without a restart.
    parts = [p.strip() for p in re.split(r"[\n,]+", body.proxy_urls or "") if p.strip()]
    crawler = ctx.config.crawler
    crawler.proxy_urls = ",".join(parts)
    crawler.enable_proxy = body.enable_proxy
    crawler.allow_fallback_on_proxy_miss = body.allow_fallback_on_proxy_miss
    return _current_proxy_config()


def _current_notify_config() -> NotifyConfig:
    notify = ctx.config.notify
    return NotifyConfig(
        desktop_toast=notify.desktop_toast,
        webhook_url=notify.webhook_url,
    )


@router.get(
    "/notify-config",
    summary="Get the export finish-notification settings",
    dependencies=[Security(ensure_user)],
)
def get_notify_config() -> NotifyConfig:
    return _current_notify_config()


@router.post(
    "/notify-config",
    summary="Update the export finish-notification settings",
    dependencies=[Security(ensure_user)],
)
def set_notify_config(
    body: NotifyConfigRequest = Body(),
) -> NotifyConfig:
    notify = ctx.config.notify
    notify.desktop_toast = body.desktop_toast
    notify.webhook_url = body.webhook_url.strip()
    return _current_notify_config()


@router.delete("/{job_id}", summary="Deletes a job")
def delete_job(
    user: User = Security(ensure_user),
    job_id: str = Path(),
) -> bool:
    ctx.jobs.verify_access(user, job_id)
    ctx.scheduler.stop_job(job_id)
    ctx.jobs.delete(job_id)
    return True


@router.get("/{job_id}", summary="Gets job details")
def get_job(
    job_id: str = Path(),
) -> Job:
    return ctx.jobs.get(job_id)


@router.post("/{job_id}/cancel", summary="Cancel a job")
def cancel_job(
    user: User = Security(ensure_user),
    job_id: str = Path(),
) -> bool:
    user_id = ctx.jobs.verify_access(user, job_id)
    who = "user" if user.id == user_id else "admin"
    ctx.scheduler.stop_job(job_id)
    ctx.jobs.cancel(job_id, who)
    return True


@router.post("/{job_id}/replay", summary="Replay a job")
def replay_job(
    user: User = Security(ensure_user),
    job_id: str = Path(),
) -> Job:
    job = ctx.jobs.get(job_id)
    data = dict(**job.extra)
    if job.type in (JobType.SEARCH_ALL_SOURCES, JobType.SEARCH_SOURCE):
        data["search_results"] = []
    if job.type == JobType.DISCOVER_SOURCE:
        data["discovered"] = []
    return ctx.jobs._create(
        user=user,
        data=data,
        type=job.type,
        depends_on=job.depends_on,
    )


@router.post("/create/fetch-novels", summary="Create a job to fetch multiple novels")
def fetch_novels(
    user: User = Security(ensure_user),
    body: FetchNovelsRequest = Body(),
) -> Job:
    urls = [str(url) for url in body.urls]
    if not urls:
        raise ServerErrors.no_novels_to_download
    if len(urls) == 1:
        return ctx.jobs.fetch_novel(user, urls[0], full=body.full)
    if body.full and not ctx.tier.full_novel_batch_allowed(user):
        raise ServerErrors.full_novel_not_allowed
    return ctx.jobs.fetch_many_novels(user, *urls, full=body.full)


@router.post(
    "/create/fetch-volumes", summary="Create a job to fetch all chapter contents for the volumes"
)
def fetch_volumes(
    user: User = Security(ensure_user),
    body: FetchVolumesRequest = Body(),
) -> Job:
    volumes = list(set(body.volumes))
    if not volumes:
        raise ServerErrors.no_volumes_to_download
    if len(volumes) == 1:
        return ctx.jobs.fetch_volume(user, volumes[0])
    return ctx.jobs.fetch_many_volumes(user, *volumes)


@router.post("/create/fetch-chapters", summary="Create a job to fetch chapter contents")
def fetch_chapters(
    user: User = Security(ensure_user),
    body: FetchChaptersRequest = Body(),
) -> Job:
    chapters = list(set(body.chapters))
    if not chapters:
        raise ServerErrors.no_chapters_to_download
    if len(chapters) == 1:
        return ctx.jobs.fetch_chapter(user, chapters[0])
    return ctx.jobs.fetch_many_chapters(user, *chapters)


@router.post("/create/fetch-images", summary="Create a job to fetch chapter images")
def fetch_images(
    user: User = Security(ensure_user),
    body: FetchImagesRequest = Body(),
) -> Job:
    images = list(set(body.images))
    if not images:
        raise ServerErrors.no_images_to_download
    if len(images) == 1:
        return ctx.jobs.fetch_image(user, images[0])
    return ctx.jobs.fetch_many_images(user, *images)


@router.post("/create/make-artifacts", summary="Create a job to make novel artifacts")
def make_artifacts(
    user: User = Security(ensure_user),
    body: MakeArtifactsRequest = Body(),
) -> Job:
    formats = list(set(body.formats) & ctx.tier.enabled_formats(user))
    if len(formats) == 0:
        raise ServerErrors.no_artifacts_to_create
    return ctx.jobs.make_many_artifacts(
        user,
        body.novel_id,
        *formats,
        language=body.language,
    )


@router.post(
    "/create/fetch-missing",
    summary="Create a job to fetch missing (not yet downloaded) chapters for a novel",
)
def fetch_missing_chapters(
    user: User = Security(ensure_user),
    body: FetchMissingChaptersRequest = Body(),
) -> Job:
    return ctx.jobs.fetch_missing_chapters(user, body.novel_id)


@router.post(
    "/create/fetch-latest",
    summary="Refresh metadata, download missing chapters, then make artifacts",
)
def fetch_latest(
    user: User = Security(ensure_user),
    body: FetchLatestRequest = Body(),
) -> Job:
    return ctx.jobs.fetch_latest(user, body.novel_id)


@router.post("/create/translate-novels", summary="Create a job to translate novels")
def translate_novels(
    user: User = Security(ensure_user),
    body: TranslateNovelsRequest = Body(),
) -> Job:
    if not ctx.tier.translation_enabled(user):
        raise ServerErrors.tier_not_allowed
    novel_ids = list(set(body.novel_ids))
    if not novel_ids:
        raise ServerErrors.no_novels_to_download
    if len(novel_ids) == 1:
        return ctx.jobs.translate_novel(user, novel_ids[0], body.language, full=body.full)
    return ctx.jobs.translate_many_novels(user, *novel_ids, language=body.language, full=body.full)


@router.post("/create/translate-volumes", summary="Create a job to translate volumes")
def translate_volumes(
    user: User = Security(ensure_user),
    body: TranslateVolumesRequest = Body(),
) -> Job:
    if not ctx.tier.translation_enabled(user):
        raise ServerErrors.tier_not_allowed
    volume_ids = list(set(body.volumes))
    if not volume_ids:
        raise ServerErrors.no_volumes_to_download
    if len(volume_ids) == 1:
        return ctx.jobs.translate_volume(user, volume_ids[0], body.language)
    return ctx.jobs.translate_many_volumes(user, *volume_ids, language=body.language)


@router.post("/create/translate-chapters", summary="Create a job to translate chapter contents")
def translate_chapters(
    user: User = Security(ensure_user),
    body: TranslateChaptersRequest = Body(),
) -> Job:
    if not ctx.tier.translation_enabled(user):
        raise ServerErrors.tier_not_allowed
    chapters = list(set(body.chapters))
    if not chapters:
        raise ServerErrors.no_chapters_to_download
    if len(chapters) == 1:
        return ctx.jobs.translate_chapter(user, chapters[0], body.language)
    return ctx.jobs.translate_many_chapters(user, *chapters, language=body.language)


@router.post("/create/search-sources", summary="Create a job to search novels in available sources")
def search_sources(
    user: User = Security(ensure_user),
    body: SearchSourceRequest = Body(),
) -> Job:
    if body.domain:
        return ctx.jobs.search_single_source(user, body.query, body.domain)
    else:
        return ctx.jobs.search_all_sources(user, body.query)


@router.post(
    "/create/discover-source",
    summary="Create a job to discover and queue every novel from a source",
)
def discover_source(
    user: User = Security(ensure_user),
    body: DiscoverSourceRequest = Body(),
) -> Job:
    return ctx.jobs.discover_source(user, str(body.url), full=body.full)


@router.post(
    "/create/export-source",
    summary="Create a job that downloads every novel from a source into a single zip",
)
def export_source(
    user: User = Security(ensure_user),
    body: ExportSourceRequest = Body(),
) -> Job:
    return ctx.jobs.export_source(
        user,
        str(body.url),
        format=body.format,
        limit=body.limit,
        retries=body.retries,
        polite=body.polite,
        dedupe=body.dedupe,
        discovery_minutes=body.discovery_minutes,
        max_chapters=body.max_chapters,
        resume=body.resume,
        requests_per_sec=body.requests_per_sec,
        auto_tune=body.auto_tune,
        update_only=body.update_only,
        urls=[str(u) for u in body.urls] if body.urls else None,
    )


@router.post(
    "/create/list-source",
    summary="Create a job that lists and ranks the novels a source exposes (no download)",
)
def list_source(
    user: User = Security(ensure_user),
    body: ListSourceRequest = Body(),
) -> Job:
    return ctx.jobs.list_source(user, str(body.url))


@router.get("/{job_id}/export", summary="Download the zip produced by an export-source job")
def download_export(
    user: User = Security(ensure_user),
    job_id: str = Path(),
) -> FileResponse:
    ctx.jobs.verify_access(user, job_id)
    job = ctx.jobs.get(job_id)
    export_file = job.extra.get("export_file")
    if not export_file:
        raise ServerErrors.no_such_file
    path = ctx.files.resolve(export_file)
    if not path.is_file():
        raise ServerErrors.no_such_file
    filename = job.extra.get("export_name") or "novels.zip"
    return FileResponse(path, media_type="application/zip", filename=filename)
