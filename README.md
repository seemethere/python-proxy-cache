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
hosts for `/artifacts/` rewrite; keep `nginx/nginx.conf` `map $artifact_host` in sync),
`REWRITE_ARTIFACT_URLS` (default `true`), `REDIS_URL`, `CACHE_BACKEND`, `CACHE_TTL_SECONDS`.
Set `REWRITE_ARTIFACT_URLS=false` to leave ordinary artifact downloads pointed at the upstream
host. Links advertising metadata are still rewritten because generated `.metadata` responses
are served by this proxy.

`METADATA_PENDING_CACHE_TTL_SECONDS` defaults to 30 seconds. This collapses an active resolver
burst in the outer cache while background metadata discovery waits for its idle window.
When a client advertises both JSON and `text/html`, an upstream HTML response is passed through
instead of being parsed and synthesized as JSON; JSON-only clients still receive JSON.

With `CACHE_BACKEND=redis_required`, startup waits for Redis for a bounded number of attempts
before exiting. `REDIS_STARTUP_MAX_ATTEMPTS` (default 30) sets the total number of connection
attempts and `REDIS_STARTUP_RETRY_DELAY_SECONDS` (default 1) sets the delay between attempts.
Optional Redis mode (`CACHE_BACKEND=redis`) still starts immediately in degraded in-memory mode.

For legacy test: point `UPSTREAM_SIMPLE_URL` at a `pypiserver` that only returns HTML - JSON will be synthesized.
Or use the compose profile:

```bash
UPSTREAM_SIMPLE_URL=http://legacy-simple:9000/simple docker compose --profile legacy up -d
```

`ENABLE_BACKGROUND_METADATA=true` enables lazy metadata enrichment after a project miss
(off by default). The proxy first probes `HEAD …metadata`; when an allowlisted wheel has no
upstream metadata, it uses bounded range reads to extract `METADATA`, stores it by wheel SHA256,
and serves it from `wheel-url.metadata`. Set `METADATA_ARTIFACT_BASE_URL` to the nginx base URL
(for example, `http://nginx`) so those range reads share its artifact cache.
Native metadata discovery and generated-metadata extraction run in separate phases. HEAD probes
wait for `METADATA_BACKGROUND_DISCOVERY_IDLE_SECONDS` of project-request inactivity visible to
that Python process (default `90`), then speculative wheel extraction independently waits for
`METADATA_BACKGROUND_EXTRACTION_IDLE_SECONDS` (also default `90`). Set either value to `0` to
restore immediate work for that phase.
`METADATA_HEAD_CONCURRENCY` (default 10) and
`METADATA_MAX_INFLIGHT_PROJECTS` (default 4) bound discovery, while
`METADATA_MAX_PENDING_PROJECTS` (default 16) bounds scheduled discovery work. Delayed jobs retain
only a project identity and body digest and are independently bounded by
`METADATA_MAX_PENDING_EXTRACTION_PROJECTS` (default 64) and
`METADATA_MAX_INFLIGHT_EXTRACTION_PROJECTS` (default 4). `METADATA_EXTRACT_CONCURRENCY` (default 2)
bounds wheel extraction within those jobs, while `METADATA_RECOVERY_CONCURRENCY` (default 2)
reserves request-path capacity so advertised metadata can recover regardless of the background
queue or idle delay. To keep large project indexes bounded, only the newest
`METADATA_MAX_EXTRACT_FILES_PER_PROJECT` (default 32) parseable wheels missing metadata are
probed during each project pass. Pending project responses use a 30-second outer-cache TTL by
default to collapse concurrent bursts without hiding enrichment for the full project TTL; set
`METADATA_PENDING_CACHE_TTL_SECONDS=0` to disable that microcache. Once the exact enriched
response is complete, it becomes cacheable for its remaining project TTL. Generated metadata is retained for
`METADATA_CACHE_TTL_SECONDS` (default one year), while failures are retried after
`METADATA_FAILURE_TTL_SECONDS` (default one hour). An advertised generated-metadata
URL is self-healing: when its content is absent from the local cache, the proxy
rechecks for native metadata and otherwise reconstructs it with the same bounded
range reader. Shared Redis reduces duplicate work across processes but is not
required for metadata availability.
`ARTIFACT_HOST_SCHEME` (default `https`) is the scheme used to reach extra allowlisted hosts.
While enrichment is enabled, only exact completed responses receive the full remaining project
TTL. Pending responses receive the bounded microcache TTL described above; Redis remains the
longer-lived project-response cache.

## Perf

See [bench/README.md](bench/README.md). TL;DR:

- `X-Cache` reports the Python project cache (Redis or memory).
- `X-Nginx-Cache` reports nginx response caching for Simple responses and artifact slices.
- Do not combine Simple and artifact nginx statuses into one hit rate. With background
  metadata enrichment enabled, pending Simple responses use the short configured
  microcache TTL and completed responses use their remaining project TTL. `X-Cache`
  can be a header stored in an nginx response, so it indicates a Python traversal only
  when nginx forwards the request.
- `MISS+synthesis` adds page-size-dependent parse, rewrite, and cache-write work to the upstream RTT.
- Use `bench/bench.py --compare` to measure degradation vs direct upstream, and `locust` to simulate 1000s runners.

Metrics: `GET /metrics` contains Python project-cache counters. Use `X-Nginx-Cache` for
nginx artifact-cache outcomes; the Python process cannot observe nginx's final cache status.
`X-Upstream-Time` and `Server-Timing` expose upstream and synthesis timing.
