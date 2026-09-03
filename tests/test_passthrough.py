import json
import time

from httpx import Response

from app.cache import cache

JSON_UPSTREAM: dict = {
    "name": "requests",
    "files": [
        {
            "filename": "requests-2.31.0-py3-none-any.whl",
            "url": "https://files.pythonhosted.org/packages/a.whl",
            "hashes": {"sha256": "abc"},
            "requires-python": ">=3.7",
            "core-metadata": True,
        },
    ],
    "meta": {"api-version": "1.1"},
}


async def test_passthrough_when_upstream_already_has_json(client, mock_upstream, monkeypatch):
    """Upstream JSON + client JSON: URLs rewritten, every other field byte-identical."""
    scheduled: list[tuple[str, bool, int]] = []
    monkeypatch.setattr(
        "app.simple_project.schedule_metadata_enrichment",
        lambda canonical, body, *, is_json, ttl: scheduled.append((canonical, is_json, ttl)),
    )
    recorder = mock_upstream(
        lambda url, headers=None: Response(
            200,
            json=JSON_UPSTREAM,
            headers={"content-type": "application/vnd.pypi.simple.v1+json"},
        )
    )

    r = await client.get(
        "/simple/requests/", headers={"Accept": "application/vnd.pypi.simple.v1+json"}
    )
    assert r.status_code == 200
    assert r.headers.get("x-synthesis") == "0"
    data = r.json()
    assert data["files"][0]["url"] == "/artifacts/files.pythonhosted.org/packages/a.whl"
    # only the url changes; the rest of the document round-trips untouched
    data["files"][0]["url"] = "https://files.pythonhosted.org/packages/a.whl"
    assert data == JSON_UPSTREAM
    assert await cache.get("simple:requests:html") is None

    # Lazy synthesis must not restart either the primary or stale source TTL.
    source_body = await cache.get("simple:requests:json")
    assert source_body is not None
    now = time.time()
    cache._mem["simple:requests:json"] = (now + 30, source_body)
    cache._mem["simple:requests:json:stale"] = (now + 40, source_body)

    r2 = await client.get("/simple/requests/", headers={"Accept": "text/html"})
    assert r2.status_code == 200
    assert "requests-2.31.0" in r2.text
    assert "HIT" in r2.headers.get("x-cache", "")
    assert recorder.call_count == 1
    assert await cache.get("simple:requests:html") == r2.text
    assert [(canonical, is_json) for canonical, is_json, _ in scheduled] == [
        ("requests", True),
        ("requests", False),
    ]
    assert 1 <= scheduled[1][2] <= 30
    assert 1 <= (await cache.ttl("simple:requests:html") or 0) <= 30
    assert 1 <= (await cache.ttl("simple:requests:html:stale") or 0) <= 40


async def test_lazy_synthesis_does_not_commit_from_replaced_source(client, monkeypatch):
    old_body = json.dumps(JSON_UPSTREAM)
    replacement = json.loads(old_body)
    replacement["files"][0]["filename"] = "replacement-1.0-py3-none-any.whl"
    replacement_body = json.dumps(replacement)
    await cache.setex("simple:requests:json", 60, old_body)
    await cache.setex("simple:requests:json:stale", 600, old_body)

    original_commit = cache.setex_many_if_unchanged
    swapped = False

    async def replace_before_first_commit(expected, values):
        nonlocal swapped
        if not swapped:
            swapped = True
            await cache.setex("simple:requests:json", 60, replacement_body)
            await cache.setex("simple:requests:json:stale", 600, replacement_body)
        return await original_commit(expected, values)

    monkeypatch.setattr(cache, "setex_many_if_unchanged", replace_before_first_commit)

    response = await client.get("/simple/requests/", headers={"Accept": "text/html"})

    assert response.status_code == 200
    assert "replacement-1.0-py3-none-any.whl" in response.text
    assert "requests-2.31.0-py3-none-any.whl" not in response.text


async def test_passthrough_html_upstream_html_client(client, mock_upstream):
    html = (
        "<!DOCTYPE html><html><body>"
        '<a href="https://files.pythonhosted.org/packages/a.whl#sha256=abc">a.whl</a>'
        "</body></html>"
    )
    recorder = mock_upstream(
        lambda url, headers=None: Response(200, text=html, headers={"content-type": "text/html"})
    )

    r = await client.get("/simple/requests/", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert r.headers.get("x-synthesis") == "0"
    assert "a.whl" in r.text
    assert await cache.get("simple:requests:json") is None

    r2 = await client.get(
        "/simple/requests/", headers={"Accept": "application/vnd.pypi.simple.v1+json"}
    )
    assert r2.status_code == 200
    assert "HIT" in r2.headers.get("x-cache", "")
    assert recorder.call_count == 1
    assert await cache.get("simple:requests:json") == r2.text
