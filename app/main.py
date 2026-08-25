from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from packaging.utils import canonicalize_name

from app.cache import cache
from app.config import settings
from app.parse import model_to_html, model_to_json, parse_simple_html, parse_simple_json

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
    yield
    if http_client:
        await http_client.aclose()


app = FastAPI(title="python-proxy-cache", lifespan=lifespan)

# simple in-memory metrics for perf harness
metrics = {
    "requests_total": 0,
    "cache_hits": 0,
    "cache_misses": 0,
    "upstream_fetches": 0,
    "synthesis_count": 0,
}


def _want_json(accept: str | None) -> bool:
    if not accept:
        return False
    return "application/vnd.pypi.simple.v1+json" in accept


@app.get("/health")
async def health():
    redis_ok = await cache.ping()
    return {"status": "ok", "redis": redis_ok, "metrics": metrics}


@app.get("/metrics")
async def prom_metrics():
    # minimal prometheus-style + json for bench
    lines = [
        f"proxy_requests_total {metrics['requests_total']}",
        f"proxy_cache_hits {metrics['cache_hits']}",
        f"proxy_cache_misses {metrics['cache_misses']}",
        f"proxy_upstream_fetches {metrics['upstream_fetches']}",
        f"proxy_synthesis_count {metrics['synthesis_count']}",
    ]
    return PlainTextResponse("\n".join(lines), media_type="text/plain")


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
    want_json = _want_json(accept)
    cache_key = f"simple:index:{'json' if want_json else 'html'}"
    if cached := await cache.get(cache_key):
        metrics["cache_hits"] += 1
        ct = "application/vnd.pypi.simple.v1+json" if want_json else "text/html"
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
        if want_json:
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
        if want_json:
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
    want_json = _want_json(accept)
    canonical = canonicalize_name(project)
    # also handle non-canonical cache alias
    cache_key = f"simple:{canonical}:{'json' if want_json else 'html'}"
    # opposite format key for synthesis without refetch
    other_key = f"simple:{canonical}:{'html' if want_json else 'json'}"

    if cached := await cache.get(cache_key):
        metrics["cache_hits"] += 1
        ct = "application/vnd.pypi.simple.v1+json" if want_json else "text/html"
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
            if want_json:
                # other is html -> json
                proj = parse_simple_html(canonical, other_cached)
                body = json.dumps(model_to_json(proj))
                ct = "application/vnd.pypi.simple.v1+json"
            else:
                proj = parse_simple_json(json.loads(other_cached))
                body = model_to_html(proj)
                ct = "text/html"
            await cache.setex(cache_key, settings.cache_ttl_seconds, body)
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
    t0 = time.perf_counter()
    try:
        r = await http_client.get(upstream_url, headers=headers)
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"upstream error: {e}") from e
    if r.status_code == 404:
        await cache.setex(cache_key, settings.cache_404_ttl, r.text)
        raise HTTPException(status_code=404, detail=f"project {canonical} not found")
    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=r.text[:500])
    metrics["upstream_fetches"] += 1
    upstream_time = (time.perf_counter() - t0) * 1000
    ct = r.headers.get("content-type", "")
    is_upstream_json = "json" in ct

    # --- Passthrough path: upstream already has what client wants ---
    # Store verbatim body for the matching format to avoid re-serialization loss (preserves PEP 700+ fields, whitespace, order)
    # and only synthesize the opposite format. Add minimal overhead: 1 parse for opposite cache population.
    if is_upstream_json == want_json:
        # perfect match — cache verbatim and synthesize opposite lazily
        passthrough_body = r.text  # preserve upstream exactly
        # store passthrough verbatim
        await cache.setex(cache_key, settings.cache_ttl_seconds, passthrough_body)
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
            await cache.setex(other_key, settings.cache_ttl_seconds, opposite_body)
        except (ValueError, TypeError, KeyError):
            pass
        out_ct = "application/vnd.pypi.simple.v1+json" if want_json else "text/html"
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
        await cache.setex(f"simple:{canonical}:json", settings.cache_ttl_seconds, r.text)
        html_body = model_to_html(proj)
        await cache.setex(f"simple:{canonical}:html", settings.cache_ttl_seconds, html_body)
        body, out_ct = html_body, "text/html"
    else:
        proj = parse_simple_html(canonical, r.text)
        if not proj.name:
            proj.name = canonical
        await cache.setex(f"simple:{canonical}:html", settings.cache_ttl_seconds, r.text)
        json_body = json.dumps(model_to_json(proj))
        await cache.setex(f"simple:{canonical}:json", settings.cache_ttl_seconds, json_body)
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
