#!/usr/bin/env python3
"""
Perf harness: measures degradation vs direct upstream and cache hit/miss/synthesis cost.

Usage:
  pip install -e ".[bench]"
  python bench/bench.py --base http://localhost:8000 --upstream https://pypi.org --project requests --concurrency 50 --requests 1000
  python bench/bench.py --base http://localhost:8000 --compare  # compares hit vs miss vs upstream

Outputs p50/p95/p99 + throughput + cache headers.
"""

import argparse
import asyncio
import json
import statistics
import time
from collections import Counter
from urllib.parse import urlparse

import httpx
from rich.console import Console
from rich.table import Table

console = Console()


async def fetch(client, url, accept, range_header=None):
    t0 = time.perf_counter()
    headers = {"Accept": accept}
    if range_header:
        headers["Range"] = range_header
    r = await client.get(url, headers=headers)
    dt = (time.perf_counter() - t0) * 1000
    return (
        r.status_code,
        dt,
        r.headers.get("X-Cache", "-"),
        r.headers.get("X-Nginx-Cache", "-"),
        r.headers.get("X-Synthesis", "-"),
        r.headers.get("X-Upstream-Time", "-"),
    )


def cache_summary(url: str, samples: list[tuple[int, str, str]]) -> dict:
    """Summarize only the cache layer responsible for the requested path."""
    path = urlparse(url).path
    python_statuses = Counter()
    nginx_simple_statuses = Counter()
    nginx_statuses = Counter()

    for status_code, python_cache, nginx_cache in samples:
        if status_code >= 400:
            continue
        if path.startswith("/simple/"):
            if nginx_cache not in {"", "-"}:
                nginx_simple_statuses[nginx_cache] += 1
            # nginx preserves the X-Cache header stored with a cached response.
            # Count it as a Python traversal only when nginx actually forwarded
            # this request, or when benchmarking Python directly.
            if nginx_cache in {
                "",
                "-",
                "MISS",
                "BYPASS",
                "EXPIRED",
                "REVALIDATED",
            } and python_cache not in {
                "",
                "-",
            }:
                python_statuses[python_cache] += 1
        elif path.startswith(("/artifacts/", "/packages/", "/files/")) and nginx_cache not in {
            "",
            "-",
        }:
            nginx_statuses[nginx_cache] += 1

    def summarize(statuses: Counter, hit_states: set[str]) -> dict:
        lookups = sum(statuses.values())
        hits = sum(statuses[state] for state in hit_states)
        return {
            "hits": hits,
            "lookups": lookups,
            "hit_rate": hits / lookups if lookups else None,
            "statuses": dict(sorted(statuses.items())),
        }

    return {
        "python_project_cache": summarize(python_statuses, {"HIT", "HIT-synthesized"}),
        "nginx_simple_cache": summarize(
            nginx_simple_statuses,
            {"HIT", "REVALIDATED", "STALE", "UPDATING"},
        ),
        "nginx_artifact_cache": summarize(nginx_statuses, {"HIT"}),
    }


async def run_load(
    base: str,
    project: str,
    accept: str,
    concurrency: int,
    total: int,
    url: str | None = None,
    range_header: str | None = None,
    warmup: bool = True,
):
    url = url or f"{base.rstrip('/')}/simple/{project}/"
    limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(limits=limits, timeout=10) as client:
        sem = asyncio.Semaphore(concurrency)
        latencies = []
        statuses = []

        async def one():
            async with sem:
                sc, dt, python_cache, nginx_cache, _synth, _up = await fetch(
                    client, url, accept, range_header
                )
                latencies.append(dt)
                statuses.append((sc, python_cache, nginx_cache))
                return dt

        if warmup:
            await fetch(client, url, accept, range_header)
            await asyncio.sleep(0.2)
        t0 = time.perf_counter()
        await asyncio.gather(*[one() for _ in range(total)])
        elapsed = time.perf_counter() - t0
        latencies.sort()

        def pct(p):
            return latencies[int(len(latencies) * p / 100)] if latencies else 0

        cache = cache_summary(url, statuses)
        return {
            "url": url,
            "accept": accept,
            "concurrency": concurrency,
            "total": total,
            "elapsed": elapsed,
            "rps": total / elapsed if elapsed else 0,
            "p50": pct(50),
            "p95": pct(95),
            "p99": pct(99),
            "min": min(latencies) if latencies else 0,
            "max": max(latencies) if latencies else 0,
            "mean": statistics.mean(latencies) if latencies else 0,
            "errors": sum(1 for s, _, _ in statuses if s not in {200, 206}),
            # Backward-compatible alias for callers of the original Simple-only benchmark.
            "hits": cache["python_project_cache"]["hits"],
            "cache": cache,
        }


async def compare(base: str, upstream: str, project: str):
    async with httpx.AsyncClient(timeout=10) as c:
        # bust cache for MISS by using a time-based project alias via query (not cached) or just clear via hitting a fresh project
        # we use a synthetic project that is unlikely cached: project + "-bench"
        for label, url, accept in [
            (
                "cache MISS passthrough (upstream already compliant)",
                f"{base}/simple/{project}/",
                "application/vnd.pypi.simple.v1+json",
            ),
            (
                "cache HIT passthrough (X-Synthesis:0)",
                f"{base}/simple/{project}/",
                "application/vnd.pypi.simple.v1+json",
            ),
            (
                "cache HIT synthesized opposite (html from json)",
                f"{base}/simple/{project}/",
                "text/html",
            ),
            (
                "upstream direct json",
                f"{upstream}/simple/{project}/",
                "application/vnd.pypi.simple.v1+json",
            ),
            ("upstream direct html", f"{upstream}/simple/{project}/", "text/html"),
        ]:
            t0 = time.perf_counter()
            r = await c.get(url, headers={"Accept": accept})
            dt = (time.perf_counter() - t0) * 1000
            console.print(
                f"[bold]{label}[/]: {r.status_code} {dt:.1f}ms X-Cache:{r.headers.get('X-Cache', '-')} X-Nginx-Cache:{r.headers.get('X-Nginx-Cache', '-')} X-Synthesis:{r.headers.get('X-Synthesis', '-')} X-Upstream-Time:{r.headers.get('X-Upstream-Time', '-')} CT:{r.headers.get('content-type', '')[:40]} len:{len(r.content)}"
            )
        console.print(
            "\n[dim]If upstream already has JSON+core-metadata: MISS should show X-Synthesis:0 and X-Upstream-Content-Type: ...json, and total ≈ upstream + <5ms.[/]"
        )
        console.print(
            "[dim]If upstream only has HTML: MISS should show X-Synthesis:1 and synthesis cost is visible in Server-Timing.[/]"
        )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://localhost:8000")
    p.add_argument("--upstream", default="https://pypi.org")
    p.add_argument("--project", default="requests")
    p.add_argument("--concurrency", type=int, default=50)
    p.add_argument("--requests", type=int, default=500)
    p.add_argument("--url", help="full Simple or artifact URL (overrides --base/--project)")
    p.add_argument("--range", dest="range_header", help='optional Range header, e.g. "bytes=0-0"')
    p.add_argument(
        "--accept", default="application/vnd.pypi.simple.v1+json", help="Accept header to bench"
    )
    p.add_argument(
        "--compare", action="store_true", help="run single-request comparison vs upstream"
    )
    p.add_argument(
        "--no-warmup",
        action="store_true",
        help="skip the client warmup request; clear or vary server caches separately",
    )
    args = p.parse_args()

    if args.compare:
        asyncio.run(compare(args.base, args.upstream, args.project))
        return

    res = asyncio.run(
        run_load(
            args.base,
            args.project,
            args.accept,
            args.concurrency,
            args.requests,
            args.url,
            args.range_header,
            warmup=not args.no_warmup,
        )
    )
    t = Table(
        title=f"Bench {res['url']} Accept:{res['accept'][:30]} c={res['concurrency']} n={res['total']}"
    )
    t.add_column("metric")
    t.add_column("value")
    for k in ["rps", "mean", "p50", "p95", "p99", "min", "max", "elapsed", "errors"]:
        v = res[k]
        t.add_row(k, f"{v:.1f}" if isinstance(v, float) else str(v))
    for layer, summary in res["cache"].items():
        if summary["lookups"]:
            t.add_row(f"{layer}_hits", f"{summary['hits']}/{summary['lookups']}")
            t.add_row(f"{layer}_hit_rate", f"{summary['hit_rate']:.1%}")
            t.add_row(f"{layer}_statuses", json.dumps(summary["statuses"], sort_keys=True))
    console.print(t)
    # degradation check
    if res["p95"] > 100:
        console.print(
            "[yellow]p95 >100ms - check the applicable cache layer (Python/Redis for Simple; nginx for artifacts)[/]"
        )
    else:
        console.print("[green]p95 OK for cache HIT path[/]")
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
