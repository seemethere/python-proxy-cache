# Perf harness

## Quick bench (no extra deps beyond httpx+rich)

```bash
pip install -e ".[bench]"
# 1. start stack
docker compose up -d

# 2. single-request comparison (shows synthesis overhead)
python bench/bench.py --base http://localhost:8000 --compare --project requests
# expect: cache HIT 1-5ms, MISS 30-80ms (upstream fetch + parse), upstream direct 20-60ms

# 3. load test - measures degradation at scale
python bench/bench.py --base http://localhost:8000 --concurrency 50 --requests 1000 --project requests
python bench/bench.py --base http://localhost:8080 --concurrency 200 --requests 5000 --project urllib3  # via nginx

# 4. compare nginx vs direct python (artifacts benefit from nginx)
python bench/bench.py --base http://localhost:8000 --concurrency 100 --requests 1000 --accept "text/html"
python bench/bench.py --base http://localhost:8080 --concurrency 100 --requests 1000 --accept "text/html"
```

## Locust (thousands of runners simulation)

```bash
pip install locust
locust -f bench/locustfile.py --host http://localhost:8080 --users 1000 --spawn-rate 100 --run-time 60s --headless
# open http://localhost:8089 for UI
```

## What to watch

- `X-Cache: HIT` vs `MISS` vs `HIT-synthesized` + `Server-Timing` headers
- `/metrics` -> `proxy_cache_hits` ratio should be >95% after warmup
- `p95` for HIT should be <10ms via python direct, <5ms via nginx
- `p95` for MISS+s nthesis should be `upstream p95 + 5-15ms` (not 2x)
- If `p95 HIT` degrades, increase `python-proxy` replicas or check `redis` latency
