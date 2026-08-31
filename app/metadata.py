from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from urllib.parse import urlparse, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from packaging.utils import InvalidWheelFilename, parse_wheel_filename
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

# Global, not per-task: a per-enrichment semaphore caps each project at N in
# flight but leaves total concurrency unbounded across projects, so a CI burst
# over 200 projects would put 200*N HEADs on upstream at once.
_head_semaphore = asyncio.Semaphore(settings.metadata_head_concurrency)
_extract_semaphore = asyncio.Semaphore(settings.metadata_extract_concurrency)
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


def _metadata_url_key(file_url: str) -> str:
    return f"metadata:url:{_artifact_url_digest(file_url)}:current"


async def store_extracted_metadata(
    file_url: str, wheel_sha256: str, content: bytes
) -> dict[str, str]:
    """Persist extracted metadata by wheel digest and associate its artifact URL."""
    wheel_sha256 = wheel_sha256.lower()
    if not _SHA256_RE.fullmatch(wheel_sha256):
        raise ValueError("wheel SHA256 must be 64 hexadecimal characters")
    text = content.decode("utf-8")
    metadata_sha256 = hashlib.sha256(content).hexdigest()
    ttl = settings.metadata_cache_ttl_seconds
    await cache.setex(_metadata_blob_key(file_url, wheel_sha256), ttl, text)
    await cache.setex(
        _metadata_url_key(file_url),
        ttl,
        json.dumps(
            {"wheel-sha256": wheel_sha256, "metadata-sha256": metadata_sha256},
            separators=(",", ":"),
        ),
    )
    return {"sha256": metadata_sha256}


async def _associate_metadata_url(file_url: str, wheel_sha256: str, metadata_sha256: str) -> None:
    await cache.setex(
        _metadata_url_key(file_url),
        settings.metadata_cache_ttl_seconds,
        json.dumps(
            {"wheel-sha256": wheel_sha256, "metadata-sha256": metadata_sha256},
            separators=(",", ":"),
        ),
    )


async def load_metadata_for_url(file_url: str) -> tuple[str, str] | None:
    """Resolve an artifact URL to (metadata text, metadata SHA256)."""
    raw_record = await cache.get(_metadata_url_key(file_url))
    if raw_record is None:
        return None
    try:
        record = json.loads(raw_record)
        wheel_sha256 = record["wheel-sha256"]
        expected = record["metadata-sha256"]
        if not _SHA256_RE.fullmatch(wheel_sha256) or not _SHA256_RE.fullmatch(expected):
            return None
    except (TypeError, KeyError, ValueError, json.JSONDecodeError):
        return None
    body = await cache.get(_metadata_blob_key(file_url, wheel_sha256))
    if body is None or hashlib.sha256(body.encode()).hexdigest() != expected:
        return None
    return body, expected


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


def schedule_metadata_enrichment(canonical: str, body: str, *, is_json: bool, ttl: int) -> None:
    if not settings.enable_background_metadata:
        return
    if len(_pending) >= max(0, settings.metadata_max_pending_projects):
        metrics["metadata_enrichment_dropped"] += 1
        logger.warning("metadata enrichment queue full; dropping %s", canonical)
        return
    task = asyncio.create_task(_enrich(canonical, body, is_json=is_json, ttl=ttl))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def drain_metadata_tasks() -> None:
    """Wait for in-flight enrichment tasks (tests)."""
    while _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)


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
    if can_extract:
        cached_body = await cache.get(_metadata_blob_key(f.url, wheel_sha256))
        if cached_body is not None:
            metadata_sha256 = hashlib.sha256(cached_body.encode()).hexdigest()
            await _associate_metadata_url(f.url, wheel_sha256, metadata_sha256)
            return _with_core_metadata(f, {"sha256": metadata_sha256})
    url = metadata_head_url(f.url)
    if url is None:
        return f
    async with _head_semaphore:
        try:
            client = get_http_client()
            r = await asyncio.wait_for(client.head(url), timeout=_HEAD_TIMEOUT)
        except Exception:
            logger.debug("metadata HEAD failed for %s", _safe_url_for_log(url), exc_info=True)
            r = None
    metrics["metadata_heads"] += 1
    core_metadata: bool | dict[str, str]
    if r is not None and r.status_code == 200:
        core_metadata = True
    else:
        if not can_extract or not allow_extraction:
            return f
        artifact_identity = f"{_artifact_url_digest(f.url)}:{wheel_sha256}"
        failure_key = f"metadata:failure:{artifact_identity}"
        if await cache.get(failure_key) is not None:
            return f

        async with miss_locks.hold(f"metadata:{artifact_identity}:fill"):
            # A different project or replica-local task may have filled it while
            # this probe waited. The process lock prevents duplicate range work
            # within this worker; Redis exposes completed results to all replicas.
            cached_body = await cache.get(_metadata_blob_key(f.url, wheel_sha256))
            if cached_body is not None:
                metadata_sha256 = hashlib.sha256(cached_body.encode()).hexdigest()
                await _associate_metadata_url(f.url, wheel_sha256, metadata_sha256)
                core_metadata = {"sha256": metadata_sha256}
            else:
                if await cache.get(failure_key) is not None:
                    return f
                artifact_url = _artifact_upstream_url(f.url)
                if artifact_url is None:
                    return f
                try:
                    async with _extract_semaphore:
                        content = await _extract_wheel_metadata(artifact_url)
                    core_metadata = await store_extracted_metadata(f.url, wheel_sha256, content)
                    metrics["metadata_extractions"] += 1
                except Exception as exc:
                    # RangeNotSupportedError, malformed wheels, transport failures,
                    # and invalid UTF-8 are all retryable after the short failure TTL.
                    logger.info(
                        "metadata extraction failed for %s: %s",
                        _safe_url_for_log(artifact_url),
                        exc,
                    )
                    await cache.setex(failure_key, settings.metadata_failure_ttl_seconds, "1")
                    metrics["metadata_extraction_failures"] += 1
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
    """Select a bounded set of newest valid wheels for extraction.

    Sorting is stable, so files for the same release retain upstream order.
    Invalid/non-wheel filenames can still be HEAD-probed but are never handed
    to the ZIP extractor.
    """
    limit = max(0, settings.metadata_max_extract_files_per_project)
    ranked: list[tuple[Version, int]] = []
    for index, file in enumerate(files):
        if not file.filename.lower().endswith(".whl"):
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
            new_files = list(
                await asyncio.gather(
                    *(
                        _probe(file, allow_extraction=index in extraction_candidates)
                        for index, file in enumerate(proj.files)
                    )
                )
            )
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
