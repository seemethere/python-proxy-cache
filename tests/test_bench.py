import pytest

from bench.bench import cache_summary, run_load


def test_simple_reports_python_and_nginx_cache_separately():
    result = cache_summary(
        "http://cache/simple/demo/",
        [
            (200, "HIT", "MISS"),
            (200, "MISS", "HIT"),
            (200, "MISS", "UPDATING"),
            (200, "REVALIDATED", "EXPIRED"),
            (200, "HIT", "REVALIDATED"),
        ],
    )

    assert result["python_project_cache"] == {
        "hits": 2,
        "lookups": 3,
        "hit_rate": 2 / 3,
        "statuses": {"HIT": 2, "REVALIDATED": 1},
    }
    assert result["nginx_simple_cache"] == {
        "hits": 3,
        "lookups": 5,
        "hit_rate": 3 / 5,
        "statuses": {"EXPIRED": 1, "HIT": 1, "MISS": 1, "REVALIDATED": 1, "UPDATING": 1},
    }
    assert result["nginx_artifact_cache"]["lookups"] == 0


def test_artifact_reports_only_nginx_cache():
    result = cache_summary(
        "http://cache/artifacts/example.invalid/demo.whl",
        [
            (200, "-", "MISS"),
            (206, "-", "HIT"),
            (200, "-", "STALE"),
            (500, "-", "HIT"),
            (200, "-", "-"),
        ],
    )

    assert result["nginx_artifact_cache"] == {
        "hits": 1,
        "lookups": 3,
        "hit_rate": 1 / 3,
        "statuses": {"HIT": 1, "MISS": 1, "STALE": 1},
    }
    assert result["python_project_cache"]["lookups"] == 0
    assert result["nginx_simple_cache"]["lookups"] == 0


def test_non_cache_path_has_no_applicable_cache_layer():
    result = cache_summary("http://cache/health", [(200, "HIT", "HIT")])

    assert result["python_project_cache"]["hit_rate"] is None
    assert result["nginx_simple_cache"]["hit_rate"] is None
    assert result["nginx_artifact_cache"]["hit_rate"] is None


@pytest.mark.parametrize(("warmup", "expected_calls"), [(True, 4), (False, 3)])
async def test_run_load_can_include_or_skip_warmup(monkeypatch, warmup, expected_calls):
    calls = 0

    async def fake_fetch(client, url, accept, range_header=None):
        nonlocal calls
        calls += 1
        return 200, 1.0, "HIT", "-", "0", "-"

    monkeypatch.setattr("bench.bench.fetch", fake_fetch)

    result = await run_load(
        "http://cache",
        "demo",
        "text/html",
        concurrency=2,
        total=3,
        warmup=warmup,
    )

    assert calls == expected_calls
    assert result["total"] == 3
