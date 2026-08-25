from __future__ import annotations

import asyncio
import json
import logging
from urllib.parse import urlparse

from app.artifacts import authority_of, host_allowed, rewrite_project_urls
from app.cache import cache
from app.config import settings
from app.deps import get_http_client
from app.metrics import metrics
from app.models import File, Project
from app.parse import model_to_html, model_to_json, parse_simple_html, parse_simple_json
from app.singleflight import miss_locks

logger = logging.getLogger(__name__)

_HEAD_TIMEOUT = 5.0

# Global, not per-task: a per-enrichment semaphore caps each project at N in
# flight but leaves total concurrency unbounded across projects, so a CI burst
# over 200 projects would put 200*N HEADs on upstream at once.
_head_semaphore = asyncio.Semaphore(settings.metadata_head_concurrency)
# Same reasoning for the tasks themselves — one enrichment per miss with no cap
# lets a miss storm spawn unbounded background work with no back-pressure.
_task_semaphore = asyncio.Semaphore(settings.metadata_max_inflight_projects)
_pending: set[asyncio.Task] = set()


def _scheme_for(host: str) -> str:
    """Scheme a rewritten artifact host is reachable on.

    /artifacts/<host>/ paths have lost the original scheme, so recover it from
    upstream_files_url for that host and fall back to artifact_host_scheme for
    extra allowlisted hosts (an internal index may well be plain http).
    """
    files = urlparse(settings.upstream_files_url)
    if files.hostname and host.lower() == authority_of(files):
        return files.scheme or "https"
    return settings.artifact_host_scheme


def metadata_head_url(file_url: str) -> str | None:
    """Build the PEP 658/714 `.metadata` URL for a file link.

    Returns None when the link points somewhere we are not allowed to probe, so
    a hostile or compromised index cannot use our file links to make the proxy
    issue arbitrary outbound requests. Mirrors the /artifacts/ allowlist.
    """
    if file_url.startswith("/artifacts/"):
        rest = file_url[len("/artifacts/") :]
        host, _, path = rest.partition("/")
        if not host or not host_allowed(host):
            return None
        path = path.split("#", 1)[0].split("?", 1)[0]
        return f"{_scheme_for(host)}://{host}/{path}.metadata"

    base = file_url.split("#", 1)[0].split("?", 1)[0]
    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if not host_allowed(authority_of(parsed)):
        # Not rewritten because it is off-allowlist — do not probe it either.
        return None
    return base + ".metadata"


def schedule_metadata_enrichment(canonical: str, body: str, *, is_json: bool, ttl: int) -> None:
    if not settings.enable_background_metadata:
        return
    task = asyncio.create_task(_enrich(canonical, body, is_json=is_json, ttl=ttl))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def drain_metadata_tasks() -> None:
    """Wait for in-flight enrichment tasks (tests)."""
    while _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)


def _needs_probe(f: File) -> bool:
    # Probe when missing or synthesized false; skip when true or hash string.
    return not (
        f.core_metadata is True
        or isinstance(f.core_metadata, str)
        or f.dist_info_metadata is True
        or isinstance(f.dist_info_metadata, str)
    )


async def _probe(f: File) -> File:
    if not _needs_probe(f):
        return f
    url = metadata_head_url(f.url)
    if url is None:
        return f
    async with _head_semaphore:
        try:
            client = get_http_client()
            r = await asyncio.wait_for(client.head(url), timeout=_HEAD_TIMEOUT)
        except Exception:
            logger.debug("metadata HEAD failed for %s", url, exc_info=True)
            metrics["metadata_heads"] += 1
            return f
    metrics["metadata_heads"] += 1
    if r.status_code != 200:
        return f
    return File(
        filename=f.filename,
        url=f.url,
        hashes=f.hashes,
        requires_python=f.requires_python,
        yanked=f.yanked,
        dist_info_metadata=f.dist_info_metadata,
        core_metadata=True,
        size=f.size,
        upload_time=f.upload_time,
    )


async def _enrich(canonical: str, body: str, *, is_json: bool, ttl: int) -> None:
    try:
        async with _task_semaphore:
            if is_json:
                proj = parse_simple_json(json.loads(body))
            else:
                proj = parse_simple_html(canonical, body)
            if not proj.name:
                proj.name = canonical

            new_files = list(await asyncio.gather(*(_probe(f) for f in proj.files)))
            changed = any(
                nf.core_metadata is True and _needs_probe(of)
                for nf, of in zip(new_files, proj.files, strict=True)
            )
            if not changed:
                return

            proj = rewrite_project_urls(Project(name=proj.name, files=new_files))
            json_body = json.dumps(model_to_json(proj))
            html_body = model_to_html(proj)
            stale = settings.cache_stale_ttl_seconds
            source_key = f"simple:{canonical}:{'json' if is_json else 'html'}"

            # Probing is slow, so a normal refill may have replaced the entry we
            # started from. Take the project's fill lock and re-check before
            # writing, so a stale enrichment cannot clobber a newer body.
            async with miss_locks.hold(f"simple:{canonical}:fill"):
                if await cache.get(source_key) != body:
                    logger.debug("skipping stale enrichment for %s", canonical)
                    return
                # Reuse the TTL the response was cached under; recomputing from the
                # default would extend an upstream max-age we already honoured.
                await cache.setex(f"simple:{canonical}:json", ttl, json_body)
                await cache.setex(f"simple:{canonical}:json:stale", stale, json_body)
                await cache.setex(f"simple:{canonical}:html", ttl, html_body)
                await cache.setex(f"simple:{canonical}:html:stale", stale, html_body)
            metrics["metadata_enrichments"] += 1
    except Exception:
        logger.exception("background metadata enrichment failed for %s", canonical)
