from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
from packaging.version import Version

from app.artifacts import authority_of, host_allowed, rewrite_file_url
from app.cache import cache
from app.config import settings
from app.deps import get_http_client
from app.metrics import metrics
from app.models import File
from app.parse import (
    metadata_value_to_html,
    parse_simple_html,
    parse_simple_json,
)
from app.singleflight import miss_locks

logger = logging.getLogger(__name__)

_HEAD_TIMEOUT = 5.0

# Bound the complete per-file probe, including Redis lookups. Limiting only the
# upstream HEAD would still let a large project open one cache operation per
# file concurrently before any task reached the HEAD limit.
_probe_semaphore = asyncio.Semaphore(settings.metadata_head_concurrency)
_cache_semaphore = asyncio.Semaphore(settings.metadata_head_concurrency)
_extract_semaphore = asyncio.Semaphore(settings.metadata_extract_concurrency)
_recovery_extract_semaphore = asyncio.Semaphore(settings.metadata_recovery_concurrency)
# Same reasoning for the tasks themselves — one enrichment per miss with no cap
# lets a miss storm spawn unbounded background work with no back-pressure.
_task_semaphore = asyncio.Semaphore(settings.metadata_max_inflight_projects)
_pending: dict[str, asyncio.Task] = {}
_extraction_project_semaphore = asyncio.Semaphore(
    settings.metadata_max_inflight_extraction_projects
)
_extraction_pending: dict[tuple[str, bool, str], asyncio.Task] = {}
_last_project_activity = 0.0
_closing = False


def _log_queue_drop(metric: str, phase: str) -> None:
    """Count every drop while logging only exponentially sparse summaries."""
    metrics[metric] += 1
    count = metrics[metric]
    if count & (count - 1) == 0:
        logger.warning("metadata %s queue full; %d total drops", phase, count)


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
        rest = file_url[len("/artifacts/") :].split("#", 1)[0]
        host, separator, path_and_query = rest.partition("/")
        if not separator or not host or not host_allowed(host):
            return None
        return _append_metadata_suffix(f"{_scheme_for(host)}://{host}/{path_and_query}")

    base = file_url.split("#", 1)[0]
    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if not host_allowed(authority_of(parsed)):
        # Not rewritten because it is off-allowlist — do not probe it either.
        return None
    return _append_metadata_suffix(base)


_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def _append_metadata_suffix(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path + ".metadata", parts.query, ""))


def _normalise_artifact_url(file_url: str) -> str:
    """Stable storage identity; signed query parameters are intentionally omitted."""
    return file_url.split("#", 1)[0].split("?", 1)[0]


def _safe_url_for_log(url: str) -> str:
    """Remove credentials, query parameters, and fragments from a logged URL."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    authority = f"{host}:{port}" if port is not None else host
    return urlunsplit((parts.scheme, authority, parts.path, "", ""))


def _artifact_url_digest(file_url: str) -> str:
    return hashlib.sha256(_normalise_artifact_url(file_url).encode()).hexdigest()


def _metadata_blob_key(file_url: str, wheel_sha256: str) -> str:
    return f"metadata:url:{_artifact_url_digest(file_url)}:sha256:{wheel_sha256.lower()}"


def _metadata_content_key(metadata_sha256: str) -> str:
    return f"metadata:content:sha256:{metadata_sha256.lower()}"


def _metadata_url_key(file_url: str) -> str:
    return f"metadata:url:{_artifact_url_digest(file_url)}:current"


def _metadata_failure_key(file_url: str, wheel_sha256: str | None) -> str:
    identity = f"metadata:failure:url:{_artifact_url_digest(file_url)}"
    if wheel_sha256 is not None:
        return f"{identity}:wheel-sha256:{wheel_sha256.lower()}"
    return f"{identity}:recovery"


def _metadata_fill_lock_key(file_url: str) -> str:
    return f"metadata:url:{_artifact_url_digest(file_url)}:fill"


def _project_ready_key(canonical: str, *, is_json: bool) -> str:
    output_format = "json" if is_json else "html"
    return f"simple:{canonical}:{output_format}:metadata-ready"


def _project_discovered_key(canonical: str, *, is_json: bool) -> str:
    output_format = "json" if is_json else "html"
    return f"simple:{canonical}:{output_format}:metadata-discovered"


def _body_digest(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


async def metadata_response_cache_ttl(canonical: str, body: str, *, is_json: bool) -> int | None:
    """Return a cache TTL only when enrichment completed for this exact body."""
    canonical = canonicalize_name(canonical)
    marker_key = _project_ready_key(canonical, is_json=is_json)
    output_format = "json" if is_json else "html"
    return await cache.ttl_if_values(
        marker_key,
        _body_digest(body),
        f"simple:{canonical}:{output_format}",
        body,
    )


async def refresh_metadata_response_readiness(
    canonical: str, body: str, *, is_json: bool, ttl: int
) -> None:
    """Extend a completed exact body's marker after successful revalidation."""
    canonical = canonicalize_name(canonical)
    marker_key = _project_ready_key(canonical, is_json=is_json)
    digest = _body_digest(body)
    body_key = f"simple:{canonical}:{'json' if is_json else 'html'}"
    await cache.setex_many_if_unchanged(
        {body_key: body, marker_key: digest},
        [(marker_key, max(settings.cache_stale_ttl_seconds, ttl), digest)],
    )


async def _store_metadata_content(
    file_url: str, content: bytes, *, wheel_sha256: str | None = None
) -> tuple[str, str]:
    text = content.decode("utf-8")
    metadata_sha256 = hashlib.sha256(content).hexdigest()
    record: dict[str, str | int] = {
        "schema": 2,
        "metadata-sha256": metadata_sha256,
    }
    if wheel_sha256 is not None:
        wheel_sha256 = wheel_sha256.lower()
        if not _SHA256_RE.fullmatch(wheel_sha256):
            raise ValueError("wheel SHA256 must be 64 hexadecimal characters")
        record["wheel-sha256"] = wheel_sha256

    ttl = settings.metadata_cache_ttl_seconds
    # Publish the URL record last: readers never observe a pointer before its
    # content has been committed. Background writes with a known wheel hash
    # also remain readable by older processes during a rolling upgrade.
    await cache.setex(_metadata_content_key(metadata_sha256), ttl, text)
    if wheel_sha256 is not None:
        await cache.setex(_metadata_blob_key(file_url, wheel_sha256), ttl, text)
    await cache.setex(
        _metadata_url_key(file_url),
        ttl,
        json.dumps(record, separators=(",", ":")),
    )
    return text, metadata_sha256


async def store_extracted_metadata(
    file_url: str, wheel_sha256: str, content: bytes
) -> dict[str, str]:
    """Persist extracted metadata by wheel digest and associate its artifact URL."""
    _, metadata_sha256 = await _store_metadata_content(file_url, content, wheel_sha256=wheel_sha256)
    return {"sha256": metadata_sha256}


async def store_recovered_metadata(file_url: str, content: bytes) -> tuple[str, str]:
    """Store request-time metadata when the wheel SHA fragment is unavailable."""
    return await _store_metadata_content(file_url, content)


async def load_metadata_for_url(
    file_url: str, *, expected_wheel_sha256: str | None = None
) -> tuple[str, str] | None:
    """Resolve an artifact URL to (metadata text, metadata SHA256)."""
    raw_record = await cache.get(_metadata_url_key(file_url))
    if raw_record is None:
        return None
    try:
        record = json.loads(raw_record)
        expected = record["metadata-sha256"]
        if not isinstance(expected, str) or not _SHA256_RE.fullmatch(expected):
            return None
    except (TypeError, KeyError, ValueError, json.JSONDecodeError):
        return None

    if record.get("schema") == 2:
        wheel_sha256 = record.get("wheel-sha256")
        if wheel_sha256 is not None and (
            not isinstance(wheel_sha256, str) or not _SHA256_RE.fullmatch(wheel_sha256)
        ):
            return None
        if expected_wheel_sha256 is not None and wheel_sha256 != expected_wheel_sha256:
            return None
        body = await cache.get(_metadata_content_key(expected))
        if body is None and isinstance(wheel_sha256, str):
            body = await cache.get(_metadata_blob_key(file_url, wheel_sha256))
    else:
        wheel_sha256 = record.get("wheel-sha256")
        if not isinstance(wheel_sha256, str) or not _SHA256_RE.fullmatch(wheel_sha256):
            return None
        if expected_wheel_sha256 is not None and wheel_sha256 != expected_wheel_sha256:
            return None
        body = await cache.get(_metadata_blob_key(file_url, wheel_sha256))
    if body is None or hashlib.sha256(body.encode()).hexdigest() != expected:
        return None
    return body, expected


async def _load_metadata_bounded(
    file_url: str, *, expected_wheel_sha256: str | None = None
) -> tuple[str, str] | None:
    async with _cache_semaphore:
        return await load_metadata_for_url(
            file_url,
            expected_wheel_sha256=expected_wheel_sha256,
        )


@dataclass(frozen=True)
class _MetadataResolution:
    body: str | None = None
    metadata_sha256: str | None = None
    native: bool = False


def _artifact_upstream_url(file_url: str) -> str | None:
    """Turn an allowlisted rewritten artifact URL back into a fetchable URL."""
    # The fragment is client-only, but the query may carry an origin signature
    # and must survive the range fetch.
    base = file_url.split("#", 1)[0]
    if base.startswith("/artifacts/"):
        rest = base[len("/artifacts/") :]
        host, sep, path = rest.partition("/")
        if not sep or not host or not host_allowed(host):
            return None
        if settings.metadata_artifact_base_url:
            return f"{settings.metadata_artifact_base_url.rstrip('/')}{base}"
        return f"{_scheme_for(host)}://{host}/{path}"

    parsed = urlparse(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if not host_allowed(authority_of(parsed)):
        return None
    return base


async def _extract_wheel_metadata(url: str) -> bytes:
    # Kept behind this tiny boundary so extraction policy/storage does not leak
    # into the range-aware ZIP reader.
    from app.wheel_metadata import extract_wheel_metadata

    return await extract_wheel_metadata(get_http_client(), url)


async def _resolve_metadata_for_url(
    file_url: str,
    *,
    wheel_sha256: str | None,
    allow_extraction: bool,
    check_native: bool = True,
    recovery: bool = False,
) -> _MetadataResolution | None:
    stored = await _load_metadata_bounded(
        file_url,
        expected_wheel_sha256=wheel_sha256,
    )
    if stored is not None:
        return _MetadataResolution(body=stored[0], metadata_sha256=stored[1])

    # Waiters take only the lightweight URL lock. Cache hits never queue behind
    # network probes, and the probe permit is released before extraction.
    async with miss_locks.hold(_metadata_fill_lock_key(file_url)):
        stored = await _load_metadata_bounded(
            file_url,
            expected_wheel_sha256=wheel_sha256,
        )
        if stored is not None:
            return _MetadataResolution(body=stored[0], metadata_sha256=stored[1])
        if recovery:
            metrics["metadata_recovery_attempts"] += 1

        if check_native:
            native_url = metadata_head_url(file_url)
            if native_url is None:
                if recovery:
                    metrics["metadata_recovery_failures"] += 1
                return None
            async with _probe_semaphore:
                try:
                    response = await asyncio.wait_for(
                        get_http_client().head(native_url, follow_redirects=False),
                        timeout=_HEAD_TIMEOUT,
                    )
                except Exception:
                    logger.debug(
                        "metadata HEAD failed for %s",
                        _safe_url_for_log(native_url),
                        exc_info=True,
                    )
                    response = None
            metrics["metadata_heads"] += 1
            if response is not None and response.status_code == 200:
                if recovery:
                    metrics["metadata_recovery_native_fallbacks"] += 1
                return _MetadataResolution(native=True)
        if not allow_extraction:
            return None

        failure_key = _metadata_failure_key(file_url, wheel_sha256)
        if await cache.get(failure_key) is not None:
            if recovery:
                metrics["metadata_recovery_failures"] += 1
            return None
        artifact_url = _artifact_upstream_url(file_url)
        if artifact_url is None:
            if recovery:
                metrics["metadata_recovery_failures"] += 1
            return None
        try:
            extraction_limit = _recovery_extract_semaphore if recovery else _extract_semaphore
            async with extraction_limit:
                content = await _extract_wheel_metadata(artifact_url)
            if wheel_sha256 is None:
                body, metadata_sha256 = await store_recovered_metadata(file_url, content)
            else:
                advertised = await store_extracted_metadata(file_url, wheel_sha256, content)
                body = content.decode("utf-8")
                metadata_sha256 = advertised["sha256"]
            metrics["metadata_extractions"] += 1
            if recovery:
                metrics["metadata_recovery_successes"] += 1
            return _MetadataResolution(body=body, metadata_sha256=metadata_sha256)
        except Exception as exc:
            logger.info(
                "metadata extraction failed for %s: %s",
                _safe_url_for_log(artifact_url),
                exc,
            )
            await cache.setex(
                failure_key,
                settings.metadata_failure_ttl_seconds,
                "1",
            )
            metrics["metadata_extraction_failures"] += 1
            if recovery:
                metrics["metadata_recovery_failures"] += 1
            return None


async def load_or_recover_metadata_for_url(file_url: str) -> tuple[str, str] | None:
    """Load generated metadata or safely reconstruct it after a cache-local miss."""
    if not settings.enable_background_metadata:
        return await _load_metadata_bounded(file_url)

    resolved = await _resolve_metadata_for_url(
        file_url,
        wheel_sha256=None,
        allow_extraction=True,
        recovery=True,
    )
    if resolved is None:
        return None
    if resolved.native:
        return None
    if resolved.body is None or resolved.metadata_sha256 is None:
        return None
    return resolved.body, resolved.metadata_sha256


def schedule_metadata_enrichment(canonical: str, body: str, *, is_json: bool, ttl: int) -> None:
    if not settings.enable_background_metadata or _closing:
        return
    note_project_request()
    canonical = canonicalize_name(canonical)
    existing = _pending.get(canonical)
    if existing is not None and not existing.done():
        return
    if len(_pending) >= max(0, settings.metadata_max_pending_projects):
        _log_queue_drop("metadata_enrichment_dropped", "enrichment")
        return
    task = asyncio.create_task(_enrich(canonical, body, is_json=is_json, ttl=ttl))
    _pending[canonical] = task

    def remove_if_current(done: asyncio.Task) -> None:
        if _pending.get(canonical) is done:
            _pending.pop(canonical, None)

    task.add_done_callback(remove_if_current)


async def ensure_metadata_background_work(canonical: str, body: str, *, is_json: bool) -> None:
    """Resume the cheapest background phase needed for an exact cached body.

    Cached responses whose discovery phase already completed should not create a
    new discovery task (and parse/probe the project again) while extraction is
    waiting for an idle window. The body-bound marker makes this shortcut safe
    across processes; changed project bodies still run discovery normally.
    """
    if not settings.enable_background_metadata or _closing:
        return
    canonical = canonicalize_name(canonical)
    digest = _body_digest(body)
    extraction_key = (canonical, is_json, digest)
    extraction = _extraction_pending.get(extraction_key)
    if extraction is not None and not extraction.done():
        return
    discovery = _pending.get(canonical)
    if discovery is not None and not discovery.done():
        return

    discovered = await cache.get(_project_discovered_key(canonical, is_json=is_json))
    if discovered == digest:
        _schedule_project_extraction(canonical, is_json=is_json, body=body)
        return

    output_format = "json" if is_json else "html"
    body_ttl = await cache.ttl(f"simple:{canonical}:{output_format}")
    if body_ttl is not None:
        schedule_metadata_enrichment(canonical, body, is_json=is_json, ttl=body_ttl)


def note_project_request() -> None:
    """Record project traffic visible to Python for the background idle gate."""
    global _last_project_activity
    if settings.enable_background_metadata:
        _last_project_activity = asyncio.get_running_loop().time()


async def drain_metadata_tasks() -> None:
    """Wait for in-flight enrichment tasks (tests)."""
    while _pending or _extraction_pending:
        discovery_tasks = list(_pending.items())
        extraction_tasks = list(_extraction_pending.items())
        await asyncio.gather(
            *(task for _, task in discovery_tasks),
            *(task for _, task in extraction_tasks),
            return_exceptions=True,
        )
        for key, task in discovery_tasks:
            if task.done() and _pending.get(key) is task:
                _pending.pop(key, None)
        for key, task in extraction_tasks:
            if task.done() and _extraction_pending.get(key) is task:
                _extraction_pending.pop(key, None)


async def cancel_metadata_tasks() -> None:
    """Cancel queued background work during application shutdown."""
    global _closing
    _closing = True
    while _pending or _extraction_pending:
        tasks = {*_pending.values(), *_extraction_pending.values()}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for key, task in list(_pending.items()):
            if task.done():
                _pending.pop(key, None)
        for key, task in list(_extraction_pending.items()):
            if task.done():
                _extraction_pending.pop(key, None)
    _pending.clear()
    _extraction_pending.clear()


def start_metadata_tasks() -> None:
    """Allow background work when a new application lifespan starts."""
    global _closing
    _closing = False


def _needs_probe(f: File) -> bool:
    # Probe when missing or synthesized false; skip when true or a hash value.
    return not (
        f.core_metadata is True
        or isinstance(f.core_metadata, (str, dict))
        or f.dist_info_metadata is True
        or isinstance(f.dist_info_metadata, (str, dict))
    )


async def _probe(f: File, *, allow_extraction: bool = True, check_native: bool = True) -> File:
    if not _needs_probe(f):
        return f
    wheel_sha256 = f.hashes.get("sha256", "").lower()
    can_extract = f.filename.lower().endswith(".whl") and bool(_SHA256_RE.fullmatch(wheel_sha256))
    resolved = await _resolve_metadata_for_url(
        f.url,
        wheel_sha256=wheel_sha256 if can_extract else None,
        allow_extraction=can_extract and allow_extraction,
        check_native=check_native,
    )
    if resolved is None:
        return f
    core_metadata: bool | dict[str, str]
    if resolved.native:
        core_metadata = True
    elif resolved.metadata_sha256 is not None:
        core_metadata = {"sha256": resolved.metadata_sha256}
    else:
        return f
    return _with_core_metadata(f, core_metadata)


def _with_core_metadata(f: File, core_metadata: bool | dict[str, str]) -> File:
    return File(
        filename=f.filename,
        url=f.url,
        hashes=f.hashes,
        requires_python=f.requires_python,
        yanked=f.yanked,
        dist_info_metadata=f.dist_info_metadata,
        core_metadata=core_metadata,
        size=f.size,
        upload_time=f.upload_time,
    )


def _advertise_metadata(body: str, *, is_json: bool, files: list[File]) -> str:
    """Add advertisements without dropping unmodelled Simple API fields."""
    if is_json:
        document = json.loads(body)
        entries = document.get("files", [])
        for entry, file in zip(entries, files, strict=False):
            if not isinstance(entry, dict) or _needs_probe(file):
                continue
            entry["core-metadata"] = file.core_metadata
            if isinstance(entry.get("url"), str):
                entry["url"] = rewrite_file_url(entry["url"])
        return json.dumps(document)

    soup = BeautifulSoup(body, "html.parser")
    anchors = [anchor for anchor in soup.find_all("a") if anchor.get("href")]
    for anchor, file in zip(anchors, files, strict=False):
        if _needs_probe(file):
            continue
        value = metadata_value_to_html(file.core_metadata)
        if value is not None:
            anchor["data-core-metadata"] = value
            anchor["href"] = rewrite_file_url(str(anchor["href"]))
    return str(soup)


def _extraction_candidate_indexes(files: list[File]) -> set[int]:
    """Select a bounded set of newest valid wheels missing metadata.

    Sorting is stable, so files for the same release retain upstream order.
    Files which already advertise upstream metadata are left untouched.
    """
    limit = max(0, settings.metadata_max_extract_files_per_project)
    ranked: list[tuple[Version, int]] = []
    for index, file in enumerate(files):
        if not _needs_probe(file) or not file.filename.lower().endswith(".whl"):
            continue
        try:
            _, version, _, _ = parse_wheel_filename(file.filename)
        except InvalidWheelFilename:
            continue
        ranked.append((version, index))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return {index for _, index in ranked[:limit]}


def _parse_project_body(canonical: str, body: str, *, is_json: bool):
    proj = parse_simple_json(json.loads(body)) if is_json else parse_simple_html(canonical, body)
    if not proj.name:
        proj.name = canonical
    return proj


def _can_extract(file: File) -> bool:
    wheel_sha256 = file.hashes.get("sha256", "").lower()
    return file.filename.lower().endswith(".whl") and bool(_SHA256_RE.fullmatch(wheel_sha256))


async def _commit_project_enrichment(
    canonical: str,
    body: str,
    *,
    is_json: bool,
    files: list[File],
    changed: bool,
    ready: bool,
) -> str | None:
    """CAS one project representation and its body-bound phase markers.

    Delayed work may outlive the current cache entry. In that case it can still
    enrich the exact stale copy without republishing it as current; a later 304
    revalidation will promote that body through the normal request path.
    """
    stale = settings.cache_stale_ttl_seconds
    source_key = f"simple:{canonical}:{'json' if is_json else 'html'}"
    stale_key = f"{source_key}:stale"
    completed_body = (
        await asyncio.to_thread(_advertise_metadata, body, is_json=is_json, files=files)
        if changed
        else body
    )

    async with miss_locks.hold(f"simple:{canonical}:fill"):
        current, stale_body = await cache.get_many([source_key, stale_key])
        expected: dict[str, str | None]
        if current == body:
            bound_key = source_key
            bound_ttl = await cache.ttl(source_key)
            expected = {source_key: body}
        elif current is None and stale_body == body:
            bound_key = stale_key
            bound_ttl = await cache.ttl(stale_key)
            # Do not race a request which refills current while background work
            # is preparing a stale-only commit.
            expected = {source_key: None, stale_key: body}
        else:
            logger.debug("skipping stale enrichment for %s", canonical)
            return None
        if bound_ttl is None:
            logger.debug("skipping expired enrichment for %s", canonical)
            return None

        marker_ttl = max(stale, bound_ttl)
        digest = _body_digest(completed_body)
        writes: list[tuple[str, int, str]] = []
        if changed:
            writes.append((bound_key, bound_ttl, completed_body))
            if bound_key == source_key:
                writes.append((stale_key, stale, completed_body))
        writes.append((_project_discovered_key(canonical, is_json=is_json), marker_ttl, digest))
        if ready:
            writes.append((_project_ready_key(canonical, is_json=is_json), marker_ttl, digest))
        committed = await cache.setex_many_if_unchanged(expected, writes)
        if not committed:
            return None
        if changed:
            metrics["metadata_enrichments"] += 1
        return completed_body


async def _wait_for_project_idle(delay: float) -> None:
    loop = asyncio.get_running_loop()
    while True:
        remaining = _last_project_activity + delay - loop.time()
        if remaining <= 0:
            return
        await asyncio.sleep(remaining)


async def _wait_for_discovery_idle() -> None:
    await _wait_for_project_idle(settings.metadata_background_discovery_idle_seconds)


async def _wait_for_extraction_idle() -> None:
    await _wait_for_project_idle(settings.metadata_background_extraction_idle_seconds)


def _schedule_project_extraction(canonical: str, *, is_json: bool, body: str) -> None:
    if _closing:
        return
    digest = _body_digest(body)
    key = (canonical, is_json, digest)
    existing = _extraction_pending.get(key)
    if existing is not None and not existing.done():
        metrics["metadata_extraction_jobs_coalesced"] += 1
        return
    if len(_extraction_pending) >= max(0, settings.metadata_max_pending_extraction_projects):
        _log_queue_drop("metadata_extraction_jobs_dropped", "extraction")
        return
    task = asyncio.create_task(
        _extract_project_after_idle(canonical, is_json=is_json, body_digest=digest)
    )
    _extraction_pending[key] = task
    metrics["metadata_extraction_jobs_queued"] += 1

    def remove_if_current(done: asyncio.Task) -> None:
        if _extraction_pending.get(key) is done:
            _extraction_pending.pop(key, None)

    task.add_done_callback(remove_if_current)


async def _extract_project_after_idle(canonical: str, *, is_json: bool, body_digest: str) -> None:
    try:
        await _wait_for_extraction_idle()
        async with _extraction_project_semaphore:
            # Activity may have resumed while this job waited for a worker.
            await _wait_for_extraction_idle()
            source_key = f"simple:{canonical}:{'json' if is_json else 'html'}"
            current, stale = await cache.get_many([source_key, f"{source_key}:stale"])
            if current is not None and _body_digest(current) == body_digest:
                body = current
            elif stale is not None and _body_digest(stale) == body_digest:
                # Metadata content is immutable and still useful to the next fill,
                # but an expired body must never be republished as current.
                body = stale
            else:
                metrics["metadata_extraction_jobs_stale"] += 1
                return

            proj = await asyncio.to_thread(_parse_project_body, canonical, body, is_json=is_json)
            candidate_indexes = sorted(_extraction_candidate_indexes(proj.files))
            new_files = list(proj.files)
            batch_size = max(1, settings.metadata_extract_concurrency)
            for offset in range(0, len(candidate_indexes), batch_size):
                await _wait_for_extraction_idle()
                batch_indexes = candidate_indexes[offset : offset + batch_size]
                probed_files = await asyncio.gather(
                    *(
                        _probe(
                            proj.files[index],
                            allow_extraction=True,
                            check_native=False,
                        )
                        for index in batch_indexes
                    )
                )
                for index, probed_file in zip(batch_indexes, probed_files, strict=True):
                    new_files[index] = probed_file
            changed = any(
                not _needs_probe(new_file) and _needs_probe(old_file)
                for new_file, old_file in zip(new_files, proj.files, strict=True)
            )
            # If only the stale copy remains, enrich it without reviving the
            # current entry. A later conditional revalidation promotes it.
            await _commit_project_enrichment(
                canonical,
                body,
                is_json=is_json,
                files=new_files,
                changed=changed,
                ready=True,
            )
            metrics["metadata_extraction_jobs_completed"] += 1
    except Exception:
        logger.exception("delayed metadata extraction failed for %s", canonical)


async def _enrich(canonical: str, body: str, *, is_json: bool, ttl: int) -> None:
    del ttl  # The exact remaining cache lifetime is sampled immediately before CAS.
    try:
        await _wait_for_discovery_idle()
        async with _task_semaphore:
            # Activity may have resumed while this job waited for a worker.
            await _wait_for_discovery_idle()
            proj = await asyncio.to_thread(_parse_project_body, canonical, body, is_json=is_json)
            candidate_indexes = sorted(_extraction_candidate_indexes(proj.files))
            digest = _body_digest(body)
            discovered = await cache.get(_project_discovered_key(canonical, is_json=is_json))

            if discovered == digest:
                if any(_can_extract(proj.files[index]) for index in candidate_indexes):
                    _schedule_project_extraction(canonical, is_json=is_json, body=body)
                else:
                    await _commit_project_enrichment(
                        canonical,
                        body,
                        is_json=is_json,
                        files=list(proj.files),
                        changed=False,
                        ready=True,
                    )
                return

            new_files = list(proj.files)
            probed_files = await asyncio.gather(
                *(_probe(proj.files[index], allow_extraction=False) for index in candidate_indexes)
            )
            for index, probed_file in zip(candidate_indexes, probed_files, strict=True):
                new_files[index] = probed_file
            changed = any(
                not _needs_probe(new_file) and _needs_probe(old_file)
                for new_file, old_file in zip(new_files, proj.files, strict=True)
            )
            needs_extraction = any(
                _needs_probe(new_files[index]) and _can_extract(new_files[index])
                for index in candidate_indexes
            )
            completed_body = await _commit_project_enrichment(
                canonical,
                body,
                is_json=is_json,
                files=new_files,
                changed=changed,
                ready=not needs_extraction,
            )
            if completed_body is None:
                return
            metrics["metadata_discovery_completions"] += 1
            if needs_extraction:
                _schedule_project_extraction(
                    canonical,
                    is_json=is_json,
                    body=completed_body,
                )
    except Exception:
        logger.exception("background metadata discovery failed for %s", canonical)
