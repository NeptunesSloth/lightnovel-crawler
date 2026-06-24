from typing import List, Optional

from pydantic import BaseModel, Field, HttpUrl

from ...dao import LanguageCode, OutputFormat


class FetchNovelsRequest(BaseModel):
    urls: List[HttpUrl] = Field(description="List of urls to fetch")
    full: bool = Field(default=False, description="To fetch all contents")


class FetchVolumesRequest(BaseModel):
    volumes: List[str] = Field(description="List of volume ids to fetch")


class FetchChaptersRequest(BaseModel):
    chapters: List[str] = Field(description="List of chapter ids to fetch")


class FetchImagesRequest(BaseModel):
    images: List[str] = Field(description="List of image ids to fetch")


class MakeArtifactsRequest(BaseModel):
    novel_id: str = Field(description="The novel id")
    formats: List[OutputFormat] = Field(description="List of formats")
    language: Optional[LanguageCode] = Field(default=None, description="Target language code")


class TranslateNovelsRequest(BaseModel):
    novel_ids: List[str] = Field(description="List of novel ids to translate")
    language: LanguageCode = Field(description="Target language code")
    full: bool = Field(default=False, description="Also translate all volumes and chapters")


class TranslateVolumesRequest(BaseModel):
    volumes: List[str] = Field(description="List of volume ids to translate")
    language: LanguageCode = Field(description="Target language code")


class TranslateChaptersRequest(BaseModel):
    chapters: List[str] = Field(description="List of chapter ids to translate")
    language: LanguageCode = Field(description="Target language code")


class FetchMissingChaptersRequest(BaseModel):
    novel_id: str = Field(description="The novel id")


class FetchLatestRequest(BaseModel):
    novel_id: str = Field(description="The novel id")


class SearchSourceRequest(BaseModel):
    query: str = Field(description="Search query", min_length=2, max_length=50)
    domain: Optional[str] = Field(default=None, description="Search in a specific source only")


class DiscoverSourceRequest(BaseModel):
    url: HttpUrl = Field(description="The source website/base URL to discover novels from")
    full: bool = Field(default=False, description="To fetch all contents of every novel found")


class ExportSourceRequest(BaseModel):
    url: HttpUrl = Field(description="The source website/base URL to export every novel from")
    format: OutputFormat = Field(
        default=OutputFormat.epub,
        description="Ebook format to generate for each novel (epub embeds manga images)",
    )
    limit: Optional[int] = Field(
        default=None, ge=1, description="Only export the first N discovered novels"
    )
    retries: int = Field(
        default=3,
        ge=0,
        le=10,
        description="Extra passes to redo novels that failed or came out incomplete",
    )
    urls: Optional[List[HttpUrl]] = Field(
        default=None,
        description="Export only these specific novels (from a browse pick-list) instead of all",
    )
    polite: bool = Field(
        default=False,
        description="Pace requests to avoid a site's rate-limiting / anti-bot blocks",
    )
    dedupe: bool = Field(
        default=True,
        description="Skip novels already in the library from another source (by title)",
    )
    discovery_minutes: int = Field(
        default=10,
        ge=1,
        le=120,
        description="Stop discovering the catalogue after this many minutes",
    )
    max_chapters: int = Field(
        default=0,
        ge=0,
        description="Cap chapters downloaded per novel (0 = no cap); keeps huge series from stalling the run",
    )


class ListSourceRequest(BaseModel):
    url: HttpUrl = Field(description="The source website/base URL to browse novels from")


class ProxyConfigRequest(BaseModel):
    proxy_urls: str = Field(
        default="",
        description=(
            "Proxy URLs to route crawler traffic through, separated by commas or newlines "
            "(e.g. http://user:pass@host:port or socks5://host:port). Blank = direct connection."
        ),
    )
    enable_proxy: bool = Field(
        default=True,
        description="Route crawler requests through the configured proxies when set",
    )
    allow_fallback_on_proxy_miss: bool = Field(
        default=True,
        description="Fall back to a direct connection if no configured proxy is reachable",
    )


class ProxyConfig(BaseModel):
    proxy_urls: str = Field(description="Current proxy URLs (comma-separated)")
    enable_proxy: bool = Field(description="Whether proxy routing is enabled")
    allow_fallback_on_proxy_miss: bool = Field(
        description="Whether direct fallback is allowed when no proxy is reachable"
    )
