# Perf harness

## Quick bench

```bash
# install with bench extras
pip install -e ".[bench]"   # or: uv sync --extra bench

# 1. start stack
docker compose up -d   # or: uvicorn app.main:app --port 8000 --reload

# 2. single-request comparison (shows synthesis overhead)
pproxy-bench --base http://localhost:8000 --compare --project requests
# also: python -m bench.bench --base http://localhost:8000 --compare
# expect: cache HIT 1-5ms, MISS 30-80ms (upstream fetch + parse), upstream direct 20-60ms

# 3. load test - measures degradation at scale
pproxy-bench --base http://localhost:8000 --concurrency 50 --requests 1000 --project requests
pproxy-bench --base http://localhost:8080 --concurrency 200 --requests 5000 --project urllib3  # via nginx

# Artifact cache: report X-Nginx-Cache independently. Range avoids downloading
# an entire large artifact for each sample.
pproxy-bench --url http://localhost:8080/artifacts/files.pythonhosted.org/path/to/package.whl \
  --range bytes=0-0 --requests 20

# 4. compare nginx vs direct python (artifacts benefit from nginx)
pproxy-bench --base http://localhost:8000 --concurrency 100 --requests 1000 --accept "text/html"
pproxy-bench --base http://localhost:8080 --concurrency 100 --requests 1000 --accept "text/html"
```

## Locust (thousands of runners simulation)

```bash
pip install locust
locust -f bench/locustfile.py --host http://localhost:8080 --users 1000 --spawn-rate 100 --run-time 60s --headless
# open http://localhost:8089 for UI
```

## What to watch

- Python project cache: `X-Cache` and `/metrics` (`proxy_cache_hits`/`proxy_cache_misses`)
- nginx artifact cache: `X-Nginx-Cache` on `/artifacts/`, `/packages/`, and `/files/`
- Keep these rates separate. In particular, background metadata enrichment makes
  `/simple/` responses intentionally `no-store` at nginx while Redis still caches them.
- `p95` for HIT should be <10ms via python direct, <5ms via nginx
- `p95` for MISS+synthesis should be `upstream p95 + 5-15ms` (not 2x)
- If `p95 HIT` degrades, increase `python-proxy` replicas or check `redis` latency
