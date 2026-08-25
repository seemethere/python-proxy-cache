# python-proxy-cache

Pull-through cache for Python package indexes that synthesizes missing `PEP 691` JSON and `PEP 658/714` metadata (`core-metadata`) when upstream only speaks `PEP 503` HTML.

Designed to sit behind your existing `nginx` fleet for thousands of CI runners.

```
runners -> nginx:8080 (cache) -> python-proxy:8000 (synthesis) -> PyPI / Nexus / Artifactory
                    |-> /simple/*     5m cache, synthesis HTML<->JSON, rewrite file URLs
                    `-> /artifacts/<host>/*  30d cache, slice, sendfile (allowlisted hosts)
```

## Quick start

```bash
docker compose up -d
curl -i http://localhost:8000/health
# JSON (PEP 691) - synthesized if upstream only has HTML
curl -H "Accept: application/vnd.pypi.simple.v1+json" http://localhost:8000/simple/requests/ | jq
# HTML (PEP 503)
curl -H "Accept: text/html" http://localhost:8000/simple/requests/ | head
# via nginx (cached)
curl -H "Accept: application/vnd.pypi.simple.v1+json" http://localhost:8080/simple/requests/ | jq

pip install --index-url http://localhost:8080/simple/ --trusted-host localhost requests
```

## Config

Env vars: `UPSTREAM_SIMPLE_URL` (default `https://pypi.org/simple`), `UPSTREAM_FILES_URL`
(default `https://files.pythonhosted.org`), `ARTIFACT_HOST_ALLOWLIST` (comma-separated extra
hosts for `/artifacts/` rewrite; keep `nginx/nginx.conf` `map $artifact_host` in sync), `REDIS_URL`, `CACHE_BACKEND`, `CACHE_TTL_SECONDS`.

For legacy test: point `UPSTREAM_SIMPLE_URL` at a `pypiserver` that only returns HTML - JSON will be synthesized.
Or use the compose profile:

```bash
UPSTREAM_SIMPLE_URL=http://legacy-simple:9000/simple docker compose --profile legacy up -d
```

`ENABLE_BACKGROUND_METADATA=true` enables lazy `HEAD …metadata` probes after a miss
(off by default; adds upstream load). Probes only ever target allowlisted artifact hosts.
`METADATA_HEAD_CONCURRENCY` (default 10) and `METADATA_MAX_INFLIGHT_PROJECTS` (default 4)
are global caps, not per-project. `ARTIFACT_HOST_SCHEME` (default `https`) is the scheme used
to reach extra allowlisted hosts.

## Perf

See [bench/README.md](bench/README.md). TL;DR:

- `X-Cache: HIT` should be 1-5ms (python) / <5ms (nginx)
- `MISS+s nthesis` = upstream RTT + 5-15ms parse overhead
- Use `bench/bench.py --compare` to measure degradation vs direct upstream, and `locust` to simulate 1000s runners.

Metrics: `GET /metrics` + `X-Cache`, `X-Upstream-Time`, `Server-Timing` headers.
