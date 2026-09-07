from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings

CacheBackend = Literal["memory", "redis", "redis_required"]


class Settings(BaseSettings):
    upstream_simple_url: str = "https://pypi.org/simple"
    upstream_files_url: str = "https://files.pythonhosted.org"
    # Comma-separated extra artifact hosts allowed for /artifacts/ rewrite (in addition to
    # the host from upstream_files_url).
    artifact_host_allowlist: str = ""
    # Rewrite allowlisted file links through /artifacts/. Disabling this leaves
    # ordinary downloads direct while still proxying links that advertise
    # metadata, since generated `.metadata` is served by this application.
    rewrite_artifact_urls: bool = True
    redis_url: str = "redis://redis:6379/0"
    cache_backend: CacheBackend = "redis"
    redis_startup_max_attempts: int = Field(default=30, ge=1)
    redis_startup_retry_delay_seconds: float = Field(default=1.0, ge=0)
    cache_ttl_seconds: int = 300  # used for /simple/ index
    cache_project_ttl_seconds: int = (
        60  # shorter TTL for /simple/{project}/ — limits stale window after publish
    )
    cache_404_ttl: int = 60
    cache_stale_ttl_seconds: int = (
        600  # how long to keep stale body/etag for conditional revalidation (304)
    )
    http_timeout: float = 10.0
    enable_background_metadata: bool = False  # HEAD .metadata for 658/714 (off by default, lazy)
    # Briefly cache incomplete project pages to collapse concurrent resolver
    # bursts without hiding newly enriched metadata for the normal project TTL.
    metadata_pending_cache_ttl_seconds: int = Field(default=30, ge=0)
    # Global cap for complete per-file metadata probes, including cache operations
    # and upstream HEADs. Without it, one large project can exhaust either pool.
    metadata_head_concurrency: int = 10
    metadata_max_inflight_projects: int = 4
    # Hard bound on scheduled enrichment tasks (running plus queued).
    metadata_max_pending_projects: int = 16
    # Wait for project traffic to become idle before native metadata discovery.
    # HEAD probes are speculative and must not compete with an active resolver.
    metadata_background_discovery_idle_seconds: float = Field(default=90.0, ge=0)
    # Wait for this much project-request inactivity observed by this process
    # before speculative wheel extraction. Zero restores immediate behavior.
    metadata_background_extraction_idle_seconds: float = Field(default=90.0, ge=0)
    # Delayed extraction retains only a project identity and body digest, not the
    # potentially large Simple response.
    metadata_max_pending_extraction_projects: int = Field(default=64, ge=0)
    metadata_max_inflight_extraction_projects: int = Field(default=4, ge=1)
    metadata_extract_concurrency: int = 2
    # Reserved request-path extraction capacity. Keeping it separate prevents a
    # speculative enrichment backlog from starving an advertised metadata URL.
    metadata_recovery_concurrency: int = 2
    # Maximum wheels extracted during one project enrichment. Upstream metadata
    # HEAD probes remain uncapped by this setting (but concurrency-bounded).
    metadata_max_extract_files_per_project: int = 32
    # Wheels are immutable and this data is content-addressed, so keep generated
    # metadata much longer than the mutable project index that advertises it.
    metadata_cache_ttl_seconds: int = 365 * 24 * 60 * 60
    # Avoid repeatedly retrying wheels that are malformed or do not support the
    # range requests required for bounded extraction.
    metadata_failure_ttl_seconds: int = 60 * 60
    # Base URL of the nginx artifact cache. When unset, extraction talks to the
    # allowlisted upstream artifact host directly.
    metadata_artifact_base_url: str = ""
    # Scheme used to reach extra allowlisted artifact hosts (the /artifacts/ path
    # has lost the original scheme); the upstream_files_url host keeps its own.
    artifact_host_scheme: Literal["https", "http"] = "https"

    def artifact_hosts(self) -> frozenset[str]:
        """Hosts eligible for /artifacts/ rewriting.

        Called once per file link, so the result is memoised against its inputs
        rather than re-parsing the URL and re-splitting the allowlist each time.
        """
        return _artifact_hosts(self.upstream_files_url, self.artifact_host_allowlist)


@lru_cache(maxsize=8)
def _artifact_hosts(upstream_files_url: str, allowlist: str) -> frozenset[str]:
    hosts: set[str] = set()
    parsed = urlparse(upstream_files_url)
    if parsed.hostname:
        # include the port when non-default so an internal files host on a custom
        # port is allowlisted as the same authority the rewrite emits
        default = {"http": 80, "https": 443}.get(parsed.scheme)
        if parsed.port is not None and parsed.port != default:
            hosts.add(f"{parsed.hostname.lower()}:{parsed.port}")
        else:
            hosts.add(parsed.hostname.lower())
    for part in allowlist.split(","):
        h = part.strip().lower()
        if h:
            hosts.add(h)
    return frozenset(hosts)


settings = Settings()
