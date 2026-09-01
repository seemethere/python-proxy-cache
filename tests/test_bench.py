from bench.bench import cache_summary


def test_simple_reports_only_python_project_cache():
    result = cache_summary(
        "http://cache/simple/demo/",
        [
            (200, "HIT", "MISS"),
            (200, "HIT-synthesized", "MISS"),
            (200, "REVALIDATED", "MISS"),
        ],
    )

    assert result["python_project_cache"] == {
        "hits": 2,
        "lookups": 3,
        "hit_rate": 2 / 3,
        "statuses": {"HIT": 1, "HIT-synthesized": 1, "REVALIDATED": 1},
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


def test_non_cache_path_has_no_applicable_cache_layer():
    result = cache_summary("http://cache/health", [(200, "HIT", "HIT")])

    assert result["python_project_cache"]["hit_rate"] is None
    assert result["nginx_artifact_cache"]["hit_rate"] is None
