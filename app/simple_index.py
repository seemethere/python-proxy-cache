from __future__ import annotations

import json
import time

import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter, Header, HTTPException, Request, Response

from app.accept import want_json as accept_wants_json
from app.cache import cache
from app.config import settings
from app.deps import get_http_client
from app.metrics import metrics

router = APIRouter()


@router.get("/simple/")
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
    http_client = get_http_client()
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
