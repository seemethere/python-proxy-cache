from __future__ import annotations

import re

import httpx

from app.config import settings


def parse_max_age(cache_control: str | None) -> int | None:
    if not cache_control:
        return None
    # e.g. "public, max-age=600" or "max-age=60, must-revalidate"
    m = re.search(r"max-age\s*=\s*(\d+)", cache_control, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


# Floor for an upstream-derived TTL. Some indexes send max-age=0 on every
# Simple response; honouring that literally turns each request into an upstream
# fetch and defeats the cache entirely. 5s keeps a stampede window without
# meaningfully extending the staleness a publisher sees.
MIN_UPSTREAM_TTL_SECONDS = 5


def effective_project_ttl(headers: dict | httpx.Headers | None) -> int:
    """Project TTL capped by upstream Cache-Control max-age when present.

    Caps only — never extends beyond settings.cache_project_ttl_seconds — and
    floors at MIN_UPSTREAM_TTL_SECONDS so max-age=0 cannot disable caching.
    """
    base = settings.cache_project_ttl_seconds
    if headers is None:
        return base
    if hasattr(headers, "get"):
        cc = headers.get("cache-control") or headers.get("Cache-Control")
    else:
        cc = None
    max_age = parse_max_age(cc)
    if max_age is not None and max_age < base:
        return max(max_age, MIN_UPSTREAM_TTL_SECONDS)
    return base
