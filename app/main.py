from __future__ import annotations

import json
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse

from app.cache import CacheState, cache
from app.config import settings
from app.metadata_route import router as metadata_router
from app.metrics import metrics, prometheus_text
from app.simple_index import router as simple_index_router
from app.simple_project import router as simple_project_router

# shared httpx client — handlers reach it via app.deps.get_http_client; tests patch it here
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
app.include_router(simple_index_router)
app.include_router(simple_project_router)
app.include_router(metadata_router)


@app.middleware("http")
async def metadata_enrichment_cache_control(request: Request, call_next):
    response = await call_next(request)
    is_project = request.url.path.startswith("/simple/") and request.url.path != "/simple/"
    if is_project:
        vary = [
            value.strip() for value in response.headers.get("vary", "").split(",") if value.strip()
        ]
        if "*" not in vary and not any(value.lower() == "accept" for value in vary):
            vary.append("Accept")
        response.headers["Vary"] = ", ".join(vary)
    # The first response can be returned before background extraction updates
    # Redis. Do not let an outer proxy retain that unenriched project page and
    # hide the completed result. The root project listing is not enriched.
    if (
        settings.enable_background_metadata
        and is_project
        and "cache-control" not in response.headers
    ):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health")
async def health():
    redis_ok = await cache.health_ping()
    ready = True
    if cache.backend == "redis_required" and not redis_ok:
        ready = False
    reported_cache_state = cache.state
    if cache.backend != "memory" and not redis_ok:
        # health_ping deliberately retains an existing client after a transient
        # failure, but the response must describe this probe rather than the
        # last successful request-path state.
        reported_cache_state = CacheState.DEGRADED
    status = "ok" if ready else "degraded"
    code = 200 if ready else 503
    return Response(
        content=json.dumps(
            {
                "status": status,
                "redis": redis_ok,
                "cache_backend": cache.backend,
                "cache_state": reported_cache_state.value,
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
