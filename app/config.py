from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic_settings import BaseSettings

CacheBackend = Literal["memory", "redis", "redis_required"]


class Settings(BaseSettings):
    upstream_simple_url: str = "https://pypi.org/simple"
    upstream_files_url: str = "https://files.pythonhosted.org"
    # Comma-separated extra artifact hosts allowed for /artifacts/ rewrite (in addition to
    # the host from upstream_files_url).
    artifact_host_allowlist: str = ""
    redis_url: str = "redis://redis:6379/0"
    cache_backend: CacheBackend = "redis"
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
