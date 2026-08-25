from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from packaging.utils import canonicalize_name

from app.accept import want_json as accept_wants_json
from app.cache import cache
from app.config import settings
from app.metrics import metrics, prometheus_text
from app.parse import model_to_html, model_to_json, parse_simple_html, parse_simple_json
from app.ttl import effective_project_ttl

# shared httpx client
http_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(
        timeout=settings.http_timeout,
        follow_redirects=True,
        headers={"User-Agent": "python-proxy-cache/0.1"},
    )
    await cache.connect()
    yield
    if http_client:
        await http_client.aclose()


app = FastAPI(title="python-proxy-cache", lifespan=lifespan)


@app.get("/health")
async def health():
    redis_ok = await cache.health_ping()
    ready = True
    if cache.backend == "redis_required" and not redis_ok:
        ready = False
    status = "ok" if ready else "degraded"
    code = 200 if ready else 503
    return Response(
        content=json.dumps(
            {
                "status": status,
                "redis": redis_ok,
                "cache_backend": cache.backend,
                "cache_state": cache.state.value,
                "metrics": metrics,
            }
        ),
        status_code=code,
        media_type="application/json",
    )


@app.get("/metrics")
async def prom_metrics():
    return PlainTextResponse(prometheus_text(), media_type="text/plain")


@app.get("/")
async def root():
    return {
        "service": "python-proxy-cache",
        "upstream": settings.upstream_simple_url,
        "endpoints": ["/simple/", "/simple/{project}/", "/health", "/metrics"],
    }


@app.get("/simple/")
async def simple_index(request: Request, accept: str | None = Header(default=None)):
    metrics["requests_total"] += 1
    wants_json = accept_wants_json(accept)
    cache_key = f"simple:index:{'json' if wants_json else 'html'}"
    if cached := await cache.get(cache_key):
        metrics["cache_hits"] += 1
        ct = "application/vnd.pypi.simple.v1+json" if wants_json else "text/html"
        return Response(cached, media_type=ct, headers={"X-Cache": "HIT", "X-Synthesis": "0"})
    metrics["cache_misses"] += 1
    # fetch upstream index (always HTML is the fallback)
    url = settings.upstream_simple_url.rstrip("/") + "/"
    headers = {"Accept": "application/vnd.pypi.simple.v1+json, text/html;q=0.9"}
    assert http_client is not None
    t0 = time.perf_counter()
    try:
        r = await http_client.get(url, headers=headers)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"upstream error: {e}") from e
    metrics["upstream_fetches"] += 1
    ct = r.headers.get("content-type", "")
    # upstream may return json or html
    if "json" in ct:
        data = r.json()
        # normalize: data has projects list?
        projects = data.get("projects", [])
        # projects may be list of dicts with name
        names = [p["name"] if isinstance(p, dict) else p for p in projects]
        if wants_json:
            body = json.dumps(
                {
                    "projects": [{"name": n} for n in sorted(set(names))],
                    "meta": {"api-version": "1.1"},
                }
            )
            await cache.setex(cache_key, settings.cache_ttl_seconds, body)
            return Response(
                body,
                media_type="application/vnd.pypi.simple.v1+json",
                headers={
                    "X-Cache": "MISS",
                    "X-Upstream-Time": f"{(time.perf_counter() - t0) * 1000:.1f}ms",
                },
            )
        else:
            # synthesize html
            html = '<!DOCTYPE html><html><head><meta name="pypi:repository-version" content="1.1"></head><body>\n'
            for n in sorted(set(names)):
                html += f'<a href="/simple/{n}/">{n}</a><br/>\n'
            html += "</body></html>"
            await cache.setex(cache_key, settings.cache_ttl_seconds, html)
            return Response(html, media_type="text/html", headers={"X-Cache": "MISS"})
    else:
        html = r.text
        if wants_json:
            # parse html to extract names, synthesize json
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "html.parser")
            names = [a.text.strip() for a in soup.find_all("a") if a.text.strip()]
            body = json.dumps(
                {
                    "projects": [{"name": n} for n in sorted(set(names))],
                    "meta": {"api-version": "1.1"},
                }
            )
            metrics["synthesis_count"] += 1
            await cache.setex(cache_key, settings.cache_ttl_seconds, body)
            return Response(
                body,
                media_type="application/vnd.pypi.simple.v1+json",
                headers={"X-Cache": "MISS", "X-Synthesis": "1"},
            )
        else:
            await cache.setex(cache_key, settings.cache_ttl_seconds, html)
            return Response(html, media_type="text/html", headers={"X-Cache": "MISS"})


@app.get("/simple/{project}/")
async def simple_project(project: str, request: Request, accept: str | None = Header(default=None)):
    start = time.perf_counter()
    metrics["requests_total"] += 1
    wants_json = accept_wants_json(accept)
    canonical = canonicalize_name(project)
    # also handle non-canonical cache alias
    cache_key = f"simple:{canonical}:{'json' if wants_json else 'html'}"
    # opposite format key for synthesis without refetch
    other_key = f"simple:{canonical}:{'html' if wants_json else 'json'}"
    project_ttl = settings.cache_project_ttl_seconds
    stale_ttl = settings.cache_stale_ttl_seconds

    not_found_key = f"simple:{canonical}:404"
    if await cache.get(not_found_key) is not None:
        metrics["cache_hits"] += 1
        raise HTTPException(
            status_code=404,
            detail=f"project {canonical} not found",
            headers={
                "X-Cache": "HIT",
                "X-Cache-Key": canonical,
            },
        )

    if cached := await cache.get(cache_key):
        metrics["cache_hits"] += 1
        ct = "application/vnd.pypi.simple.v1+json" if wants_json else "text/html"
        return Response(
            cached,
            media_type=ct,
            headers={
                "X-Cache": "HIT",
                "X-Cache-Key": canonical,
                "X-Synthesis": "0",
                "Server-Timing": f"cache;dur={(time.perf_counter() - start) * 1000:.1f}",
            },
        )

    # check if opposite format is cached -> synthesize without upstream
    if other_cached := await cache.get(other_key):
        metrics["synthesis_count"] += 1
        # parse other and convert
        try:
            if wants_json:
                # other is html -> json
                proj = parse_simple_html(canonical, other_cached)
                body = json.dumps(model_to_json(proj))
                ct = "application/vnd.pypi.simple.v1+json"
            else:
                proj = parse_simple_json(json.loads(other_cached))
                body = model_to_html(proj)
                ct = "text/html"
            await cache.setex(cache_key, project_ttl, body)
            # keep stale copy for revalidation window
            await cache.setex(f"{cache_key}:stale", stale_ttl, body)
            metrics["cache_hits"] += 1  # synthesis hit
            return Response(
                body,
                media_type=ct,
                headers={
                    "X-Cache": "HIT-synthesized",
                    "X-Synthesis": "1",
                    "Server-Timing": f"synthesis;dur={(time.perf_counter() - start) * 1000:.1f}",
                },
            )
        except (ValueError, TypeError, KeyError):
            pass

    metrics["cache_misses"] += 1
    # fetch upstream — forward client's preference but accept either
    assert http_client is not None
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
    etag_key = f"simple:{canonical}:etag"
    lastmod_key = f"simple:{canonical}:lastmod"
    if etag := await cache.get(etag_key):
        headers["If-None-Match"] = etag
    if last_mod := await cache.get(lastmod_key):
        headers["If-Modified-Since"] = last_mod

    t0 = time.perf_counter()
    try:
        r = await http_client.get(upstream_url, headers=headers)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"upstream error: {e}") from e

    # 304 Not Modified — revalidate stale body
    if r.status_code == 304:
        stale_body = await cache.get(f"{cache_key}:stale")
        if stale_body is None:
            # fallback: try opposite stale then synthesize, or the other stale directly
            other_stale = await cache.get(f"{other_key}:stale")
            if other_stale is not None:
                try:
                    if wants_json:
                        proj = parse_simple_html(canonical, other_stale)
                        stale_body = json.dumps(model_to_json(proj))
                    else:
                        proj = parse_simple_json(json.loads(other_stale))
                        stale_body = model_to_html(proj)
                    # cache synthesized stale as well
                    await cache.setex(f"{other_key}:stale", stale_ttl, other_stale)
                except (ValueError, TypeError, KeyError):
                    stale_body = None
        if stale_body is None:
            raise HTTPException(status_code=502, detail="upstream 304 but no stale cache")
        # effective TTL respects upstream Cache-Control if present on 304
        eff_ttl = effective_project_ttl(r.headers)
        await cache.setex(cache_key, eff_ttl, stale_body)
        # refresh etag/lastmod TTL
        if etag:
            await cache.setex(etag_key, stale_ttl, etag)
        if last_mod:
            await cache.setex(lastmod_key, stale_ttl, last_mod)
        # also refresh opposite stale's TTL if exists
        other_stale = await cache.get(f"{other_key}:stale")
        if other_stale:
            await cache.setex(other_key, eff_ttl, other_stale)
        metrics["cache_hits"] += 1
        metrics["upstream_fetches"] += 1
        ct = "application/vnd.pypi.simple.v1+json" if wants_json else "text/html"
        return Response(
            stale_body,
            media_type=ct,
            headers={
                "X-Cache": "REVALIDATED",
                "X-Cache-Key": canonical,
                "X-Synthesis": "0",
                "Server-Timing": f"revalidated;dur={(time.perf_counter() - start) * 1000:.1f}",
            },
        )

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
    if etag_val:
        await cache.setex(etag_key, stale_ttl, etag_val)
    if lastmod_val:
        await cache.setex(lastmod_key, stale_ttl, lastmod_val)

    # --- Passthrough path: upstream already has what client wants ---
    # Store verbatim body for the matching format to avoid re-serialization loss (preserves PEP 700+ fields, whitespace, order)
    # and only synthesize the opposite format. Add minimal overhead: 1 parse for opposite cache population.
    if is_upstream_json == wants_json:
        # perfect match — cache verbatim and synthesize opposite lazily
        passthrough_body = r.text  # preserve upstream exactly
        # store passthrough verbatim
        await cache.setex(cache_key, eff_ttl, passthrough_body)
        await cache.setex(f"{cache_key}:stale", stale_ttl, passthrough_body)
        # synthesize opposite format once for next request (cost is one parse, not on critical path for this response)
        try:
            if is_upstream_json:
                proj = parse_simple_json(r.json())
                if not proj.name:
                    proj.name = canonical
                opposite_body = model_to_html(proj)
            else:
                proj = parse_simple_html(canonical, r.text)
                if not proj.name:
                    proj.name = canonical
                opposite_body = json.dumps(model_to_json(proj))
            await cache.setex(other_key, eff_ttl, opposite_body)
            await cache.setex(f"{other_key}:stale", stale_ttl, opposite_body)
        except (ValueError, TypeError, KeyError):
            pass
        out_ct = "application/vnd.pypi.simple.v1+json" if wants_json else "text/html"
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
        # synthesize HTML for client, but also cache upstream JSON verbatim
        await cache.setex(f"simple:{canonical}:json", eff_ttl, r.text)
        await cache.setex(f"simple:{canonical}:json:stale", stale_ttl, r.text)
        html_body = model_to_html(proj)
        await cache.setex(f"simple:{canonical}:html", eff_ttl, html_body)
        await cache.setex(f"simple:{canonical}:html:stale", stale_ttl, html_body)
        body, out_ct = html_body, "text/html"
    else:
        proj = parse_simple_html(canonical, r.text)
        if not proj.name:
            proj.name = canonical
        await cache.setex(f"simple:{canonical}:html", eff_ttl, r.text)
        await cache.setex(f"simple:{canonical}:html:stale", stale_ttl, r.text)
        json_body = json.dumps(model_to_json(proj))
        await cache.setex(f"simple:{canonical}:json", eff_ttl, json_body)
        await cache.setex(f"simple:{canonical}:json:stale", stale_ttl, json_body)
        body, out_ct = json_body, "application/vnd.pypi.simple.v1+json"

    metrics["synthesis_count"] += 1
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
