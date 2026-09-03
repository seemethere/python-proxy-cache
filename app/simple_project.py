from __future__ import annotations

import json
import logging
import time

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, Response
from packaging.utils import canonicalize_name

from app.accept import want_json as accept_wants_json
from app.artifacts import rewrite_project_urls, rewrite_simple_body
from app.cache import cache
from app.config import settings
from app.deps import get_http_client
from app.metadata import (
    metadata_response_cache_ttl,
    refresh_metadata_response_readiness,
    schedule_metadata_enrichment,
)
from app.metrics import metrics
from app.parse import model_to_html, model_to_json, parse_simple_html, parse_simple_json
from app.singleflight import miss_locks
from app.ttl import effective_project_ttl

logger = logging.getLogger(__name__)

router = APIRouter()


async def _cached_response(
    body: str, *, wants_json: bool, canonical: str, start: float
) -> Response:
    ct = "application/vnd.pypi.simple.v1+json" if wants_json else "text/html"
    headers = {
        "X-Cache": "HIT",
        "X-Cache-Key": canonical,
        "X-Synthesis": "0",
        "Server-Timing": f"cache;dur={(time.perf_counter() - start) * 1000:.1f}",
    }
    if settings.enable_background_metadata:
        cache_ttl = await metadata_response_cache_ttl(canonical, body, is_json=wants_json)
        if cache_ttl is not None:
            headers["Cache-Control"] = f"public, max-age={cache_ttl}"
        else:
            output_format = "json" if wants_json else "html"
            body_ttl = await cache.ttl(f"simple:{canonical}:{output_format}")
            if body_ttl is not None:
                schedule_metadata_enrichment(canonical, body, is_json=wants_json, ttl=body_ttl)
    return Response(
        body,
        media_type=ct,
        headers=headers,
    )


async def _try_serve_from_cache(
    *, canonical: str, cache_key: str, other_key: str, wants_json: bool, start: float
) -> Response | None:
    """Serve from the negative cache, the primary key, or the opposite format.

    Returns None when only an upstream fetch can satisfy the request. Called once
    before taking the miss lock and again after acquiring it, so waiters released
    by the leader pick up the freshly filled entry instead of refetching.
    Raises HTTPException(404) on a negative-cache hit.
    """
    not_found, cached = await cache.get_many([f"simple:{canonical}:404", cache_key])
    if not_found is not None:
        metrics["cache_hits"] += 1
        raise HTTPException(
            status_code=404,
            detail=f"project {canonical} not found",
            headers={"X-Cache": "HIT", "X-Cache-Key": canonical},
        )

    if cached:
        metrics["cache_hits"] += 1
        return await _cached_response(
            cached, wants_json=wants_json, canonical=canonical, start=start
        )

    # Fetch the large opposite body only after the requested representation misses.
    # This keeps the common exact-format HIT to one body transfer from Redis.
    other_cached = await cache.get(other_key)
    if other_cached:
        try:
            if wants_json:
                proj = parse_simple_html(canonical, other_cached)
                body = json.dumps(model_to_json(rewrite_project_urls(proj)))
                ct = "application/vnd.pypi.simple.v1+json"
            else:
                proj = parse_simple_json(json.loads(other_cached))
                body = model_to_html(rewrite_project_urls(proj))
                ct = "text/html"
        except (ValueError, TypeError, KeyError):
            # unparseable cache entry — fall through to an upstream fetch
            return None
        # Sample source lifetimes after the potentially expensive conversion so
        # the target cannot regain the time spent parsing a large index.
        other_ttl = await cache.ttl(other_key)
        if other_ttl is None:
            return None
        other_stale_ttl = await cache.ttl(f"{other_key}:stale")
        writes = [(cache_key, other_ttl, body)]
        if other_stale_ttl is not None:
            writes.append((f"{cache_key}:stale", other_stale_ttl, body))
        # A source generation can be enriched or refilled while synthesis runs.
        # Commit only if it is still the exact body we converted.
        committed = await cache.setex_many_if_unchanged({other_key: other_cached}, writes)
        if not committed:
            return None
        schedule_metadata_enrichment(
            canonical,
            body,
            is_json=wants_json,
            ttl=other_ttl,
        )
        metrics["synthesis_count"] += 1
        metrics["cache_hits"] += 1
        return Response(
            body,
            media_type=ct,
            headers={
                "X-Cache": "HIT-synthesized",
                "X-Synthesis": "1",
                "Server-Timing": f"synthesis;dur={(time.perf_counter() - start) * 1000:.1f}",
            },
        )
    return None


@router.get("/simple/{project}/")
async def simple_project(project: str, request: Request, accept: str | None = Header(default=None)):
    start = time.perf_counter()
    metrics["requests_total"] += 1
    wants_json = accept_wants_json(accept)
    canonical = canonicalize_name(project)
    # also handle non-canonical cache alias
    cache_key = f"simple:{canonical}:{'json' if wants_json else 'html'}"
    # opposite format key for synthesis without refetch
    other_key = f"simple:{canonical}:{'html' if wants_json else 'json'}"
    stale_ttl = settings.cache_stale_ttl_seconds

    not_found_key = f"simple:{canonical}:404"
    serve_args = {
        "canonical": canonical,
        "cache_key": cache_key,
        "other_key": other_key,
        "wants_json": wants_json,
        "start": start,
    }
    if (hit := await _try_serve_from_cache(**serve_args)) is not None:
        return hit

    # Singleflight: one upstream fill per project. Waiters re-check the cache after
    # acquiring the lock, so a stampede collapses to a single upstream fetch.
    # Multi-replica coherence still relies on Redis + nginx proxy_cache_lock.
    async with miss_locks.hold(f"simple:{canonical}:fill"):
        if (hit := await _try_serve_from_cache(**serve_args)) is not None:
            return hit

        metrics["cache_misses"] += 1
        # fetch upstream — forward client's preference but accept either
        http_client = get_http_client()
        upstream_url = f"{settings.upstream_simple_url.rstrip('/')}/{canonical}/"
        client_accept = request.headers.get("accept", "")
        # If client explicitly wants JSON, ask upstream for JSON first; otherwise prefer what upstream has
        if client_accept:
            headers = {"Accept": client_accept}
        else:
            headers = {"Accept": "application/vnd.pypi.simple.v1+json, text/html;q=0.9"}

        # conditional GET: send etag/last-modified if we have them from stale window
        # Keys are per-canonical (not per-format) because upstream URL is same; representation
        # validation still benefits from 304 even if we synthesize opposite format.
        #
        # Only revalidate when a stale body actually survives to serve on a 304.
        # Validators are tiny and the bodies are large, so a Redis running
        # maxmemory-policy allkeys-lru evicts the bodies first — sending a validator we
        # cannot satisfy would turn that into a hard 502 for the rest of its TTL.
        etag_key = f"simple:{canonical}:etag"
        lastmod_key = f"simple:{canonical}:lastmod"
        etag = None
        last_mod = None
        have_stale = await cache.exists_any([f"{cache_key}:stale", f"{other_key}:stale"])
        if have_stale:
            etag, last_mod = await cache.get_many([etag_key, lastmod_key])
            if etag:
                headers["If-None-Match"] = etag
            if last_mod:
                headers["If-Modified-Since"] = last_mod

        t0 = time.perf_counter()
        try:
            r = await http_client.get(upstream_url, headers=headers)
        except httpx.HTTPError as e:
            raise HTTPException(status_code=502, detail=f"upstream error: {e}") from e

        # 304 Not Modified — revalidate stale body
        if r.status_code == 304:
            stale_body, other_stale = await cache.get_many(
                [f"{cache_key}:stale", f"{other_key}:stale"]
            )
            # Fallback: try the opposite stale representation and synthesize.
            if stale_body is None and other_stale is not None:
                try:
                    if wants_json:
                        proj = parse_simple_html(canonical, other_stale)
                        stale_body = json.dumps(model_to_json(rewrite_project_urls(proj)))
                    else:
                        proj = parse_simple_json(json.loads(other_stale))
                        stale_body = model_to_html(rewrite_project_urls(proj))
                    # cache synthesized stale as well
                    await cache.setex(f"{other_key}:stale", stale_ttl, other_stale)
                except (ValueError, TypeError, KeyError):
                    stale_body = None
            if stale_body is None:
                # Stale copy vanished between the have_stale check and now (TTL expiry or
                # eviction). Drop the validators and refetch unconditionally rather than
                # failing a request we can still satisfy.
                logger.warning(
                    "304 for %s with no stale body; refetching unconditionally", canonical
                )
                headers.pop("If-None-Match", None)
                headers.pop("If-Modified-Since", None)
                try:
                    r = await http_client.get(upstream_url, headers=headers)
                except httpx.HTTPError as e:
                    raise HTTPException(status_code=502, detail=f"upstream error: {e}") from e
                if r.status_code == 304:
                    # Upstream ignored the absent validators; nothing left to serve.
                    raise HTTPException(status_code=502, detail="upstream 304 but no stale cache")
            else:
                # effective TTL respects upstream Cache-Control if present on 304
                eff_ttl = effective_project_ttl(r.headers)
                refreshed_values = [(cache_key, eff_ttl, stale_body)]
                if etag:
                    refreshed_values.append((etag_key, stale_ttl, etag))
                if last_mod:
                    refreshed_values.append((lastmod_key, stale_ttl, last_mod))
                if other_stale:
                    refreshed_values.append((other_key, eff_ttl, other_stale))
                await cache.setex_many(refreshed_values)
                if settings.enable_background_metadata:
                    await refresh_metadata_response_readiness(
                        canonical, stale_body, is_json=wants_json, ttl=eff_ttl
                    )
                metrics["cache_hits"] += 1
                metrics["upstream_fetches"] += 1
                response = await _cached_response(
                    stale_body, wants_json=wants_json, canonical=canonical, start=start
                )
                response.headers["X-Cache"] = "REVALIDATED"
                response.headers["Server-Timing"] = (
                    f"revalidated;dur={(time.perf_counter() - start) * 1000:.1f}"
                )
                return response

        if r.status_code == 404:
            # Distinct key so a cached 404 never returns as a 200 HIT on the success path.
            await cache.setex(not_found_key, settings.cache_404_ttl, "1")
            raise HTTPException(
                status_code=404,
                detail=f"project {canonical} not found",
                headers={"X-Cache": "MISS", "X-Cache-Key": canonical},
            )
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=r.text[:500])
        metrics["upstream_fetches"] += 1
        upstream_time = (time.perf_counter() - t0) * 1000
        ct = r.headers.get("content-type", "")
        is_upstream_json = "json" in ct
        eff_ttl = effective_project_ttl(r.headers)

        # persist validator headers for next conditional GET
        etag_val = r.headers.get("etag") or r.headers.get("ETag")
        lastmod_val = r.headers.get("last-modified") or r.headers.get("Last-Modified")
        validator_values: list[tuple[str, int, str]] = []
        if etag_val:
            validator_values.append((etag_key, stale_ttl, etag_val))
        if lastmod_val:
            validator_values.append((lastmod_key, stale_ttl, lastmod_val))

        # --- Passthrough path: upstream already has what client wants ---
        # Store verbatim body for the matching format to avoid re-serialization loss (preserves PEP 700+ fields, whitespace, order)
        # and synthesize the opposite format only if a later client requests it.
        if is_upstream_json == wants_json:
            # perfect match — cache verbatim and synthesize opposite lazily
            # URL rewrite only — every other upstream field (PEP 700 versions,
            # PEP 708 meta.tracks/alternate-locations, PEP 740 provenance, ordering,
            # unknown keys) survives, so X-Synthesis: 0 stays truthful.
            passthrough_body = rewrite_simple_body(r.text, is_json=is_upstream_json)
            # Store only what this request needs. The opposite representation is
            # synthesized lazily by _try_serve_from_cache if a client asks for it.
            await cache.setex_many(
                [
                    *validator_values,
                    (cache_key, eff_ttl, passthrough_body),
                    (f"{cache_key}:stale", stale_ttl, passthrough_body),
                ]
            )
            out_ct = "application/vnd.pypi.simple.v1+json" if wants_json else "text/html"
            schedule_metadata_enrichment(
                canonical, passthrough_body, is_json=is_upstream_json, ttl=eff_ttl
            )
            total_ms = (time.perf_counter() - start) * 1000
            return Response(
                passthrough_body,
                media_type=out_ct,
                headers={
                    "X-Cache": "MISS",
                    "X-Upstream-Time": f"{upstream_time:.1f}ms",
                    "X-Synthesis": "0",  # no synthesis on the hot path
                    "X-Upstream-Content-Type": ct,
                    "Server-Timing": f"upstream;dur={upstream_time:.1f}, total;dur={total_ms:.1f}",
                },
            )

        # --- Synthesis path: upstream format != client want -> need to convert ---
        if is_upstream_json:
            try:
                data = r.json()
            except ValueError:
                data = json.loads(r.text)
            proj = parse_simple_json(data)
            if not proj.name:
                proj.name = canonical
            # cache the upstream JSON with URLs rewritten but all other fields intact
            json_body = rewrite_simple_body(r.text, is_json=True)
            html_body = model_to_html(rewrite_project_urls(proj))
            await cache.setex_many(
                [
                    *validator_values,
                    (f"simple:{canonical}:json", eff_ttl, json_body),
                    (f"simple:{canonical}:json:stale", stale_ttl, json_body),
                    (f"simple:{canonical}:html", eff_ttl, html_body),
                    (f"simple:{canonical}:html:stale", stale_ttl, html_body),
                ]
            )
            body, out_ct = html_body, "text/html"
        else:
            proj = parse_simple_html(canonical, r.text)
            if not proj.name:
                proj.name = canonical
            html_body = rewrite_simple_body(r.text, is_json=False)
            json_body = json.dumps(model_to_json(rewrite_project_urls(proj)))
            await cache.setex_many(
                [
                    *validator_values,
                    (f"simple:{canonical}:html", eff_ttl, html_body),
                    (f"simple:{canonical}:html:stale", stale_ttl, html_body),
                    (f"simple:{canonical}:json", eff_ttl, json_body),
                    (f"simple:{canonical}:json:stale", stale_ttl, json_body),
                ]
            )
            body, out_ct = json_body, "application/vnd.pypi.simple.v1+json"

        metrics["synthesis_count"] += 1
        schedule_metadata_enrichment(canonical, body, is_json=(out_ct != "text/html"), ttl=eff_ttl)
        total_ms = (time.perf_counter() - start) * 1000
        return Response(
            body,
            media_type=out_ct,
            headers={
                "X-Cache": "MISS",
                "X-Upstream-Time": f"{upstream_time:.1f}ms",
                "X-Synthesis": "1",
                "X-Upstream-Content-Type": ct,
                "Server-Timing": f"upstream;dur={upstream_time:.1f}, total;dur={total_ms:.1f}",
            },
        )
