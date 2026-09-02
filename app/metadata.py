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

from app.artifacts import authority_of, host_allowed, rewrite_project_urls
from app.cache import cache
from app.config import settings
from app.deps import get_http_client
from app.metrics import metrics
from app.models import File, Project
from app.parse import (
    metadata_value_to_html,
    model_to_html,
    model_to_json,
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
    if not settings.enable_background_metadata:
        return
    canonical = canonicalize_name(canonical)
    existing = _pending.get(canonical)
    if existing is not None and not existing.done():
        return
    if len(_pending) >= max(0, settings.metadata_max_pending_projects):
        metrics["metadata_enrichment_dropped"] += 1
        logger.warning("metadata enrichment queue full; dropping %s", canonical)
        return
    task = asyncio.create_task(_enrich(canonical, body, is_json=is_json, ttl=ttl))
    _pending[canonical] = task

    def remove_if_current(done: asyncio.Task) -> None:
        if _pending.get(canonical) is done:
            _pending.pop(canonical, None)

    task.add_done_callback(remove_if_current)


async def drain_metadata_tasks() -> None:
    """Wait for in-flight enrichment tasks (tests)."""
    while _pending:
        await asyncio.gather(*list(_pending.values()), return_exceptions=True)


def _needs_probe(f: File) -> bool:
    # Probe when missing or synthesized false; skip when true or a hash value.
    return not (
        f.core_metadata is True
        or isinstance(f.core_metadata, (str, dict))
        or f.dist_info_metadata is True
        or isinstance(f.dist_info_metadata, (str, dict))
    )


async def _probe(f: File, *, allow_extraction: bool = True) -> File:
    if not _needs_probe(f):
        return f
    wheel_sha256 = f.hashes.get("sha256", "").lower()
    can_extract = f.filename.lower().endswith(".whl") and bool(_SHA256_RE.fullmatch(wheel_sha256))
    resolved = await _resolve_metadata_for_url(
        f.url,
        wheel_sha256=wheel_sha256 if can_extract else None,
        allow_extraction=can_extract and allow_extraction,
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
        return json.dumps(document)

    soup = BeautifulSoup(body, "html.parser")
    anchors = [anchor for anchor in soup.find_all("a") if anchor.get("href")]
    for anchor, file in zip(anchors, files, strict=False):
        if _needs_probe(file):
            continue
        value = metadata_value_to_html(file.core_metadata)
        if value is not None:
            anchor["data-core-metadata"] = value
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


async def _enrich(canonical: str, body: str, *, is_json: bool, ttl: int) -> None:
    try:
        async with _task_semaphore:
            if is_json:
                proj = parse_simple_json(json.loads(body))
            else:
                proj = parse_simple_html(canonical, body)
            if not proj.name:
                proj.name = canonical

            extraction_candidates = _extraction_candidate_indexes(proj.files)
            new_files = list(proj.files)
            candidate_indexes = sorted(extraction_candidates)
            probed_files = await asyncio.gather(
                *(_probe(proj.files[index]) for index in candidate_indexes)
            )
            for index, probed_file in zip(candidate_indexes, probed_files, strict=True):
                new_files[index] = probed_file
            changed = any(
                not _needs_probe(nf) and _needs_probe(of)
                for nf, of in zip(new_files, proj.files, strict=True)
            )
            if not changed:
                return

            stale = settings.cache_stale_ttl_seconds
            source_key = f"simple:{canonical}:{'json' if is_json else 'html'}"

            # Probing is slow, so a normal refill may have replaced the entry we
            # started from. Take the project's fill lock and re-check before
            # writing, so a stale enrichment cannot clobber a newer body.
            async with miss_locks.hold(f"simple:{canonical}:fill"):
                if await cache.get(source_key) != body:
                    logger.debug("skipping stale enrichment for %s", canonical)
                    return
                json_current = await cache.get(f"simple:{canonical}:json")
                html_current = await cache.get(f"simple:{canonical}:html")
                enriched = rewrite_project_urls(Project(name=proj.name, files=new_files))
                json_body = (
                    _advertise_metadata(json_current, is_json=True, files=new_files)
                    if json_current is not None
                    else json.dumps(model_to_json(enriched))
                )
                html_body = (
                    _advertise_metadata(html_current, is_json=False, files=new_files)
                    if html_current is not None
                    else model_to_html(enriched)
                )
                # Reuse the TTL the response was cached under; recomputing from the
                # default would extend an upstream max-age we already honoured.
                await cache.setex(f"simple:{canonical}:json", ttl, json_body)
                await cache.setex(f"simple:{canonical}:json:stale", stale, json_body)
                await cache.setex(f"simple:{canonical}:html", ttl, html_body)
                await cache.setex(f"simple:{canonical}:html:stale", stale, html_body)
            metrics["metadata_enrichments"] += 1
    except Exception:
        logger.exception("background metadata enrichment failed for %s", canonical)
