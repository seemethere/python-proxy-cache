from __future__ import annotations

from httpx import Response

from app.cache import cache
from app.config import settings
from app.metadata import drain_metadata_tasks

HTML = (
    "<!DOCTYPE html><html><body>"
    '<a href="https://files.pythonhosted.org/packages/a.whl#sha256=abc">a.whl</a>'
    "</body></html>"
)


async def test_200_stores_etag_for_conditional_get(client, mock_upstream):
    rec = mock_upstream(
        lambda url, headers=None: Response(
            200,
            text=HTML,
            headers={
                "content-type": "text/html",
                "etag": '"v1"',
                "last-modified": "Mon, 01 Jan 2024 00:00:00 GMT",
            },
        )
    )
    r = await client.get("/simple/demo/", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert rec.call_count == 1
    assert await cache.get("simple:demo:etag") == '"v1"'
    assert await cache.get("simple:demo:lastmod") == "Mon, 01 Jan 2024 00:00:00 GMT"


async def test_revalidate_304_with_etag(client, mock_upstream):
    state = {"n": 0}

    def handler(url, headers=None):
        state["n"] += 1
        headers = headers or {}
        if state["n"] == 1:
            return Response(
                200,
                text=HTML,
                headers={"content-type": "text/html", "etag": '"v1"'},
            )
        # second call should be conditional
        assert headers.get("If-None-Match") == '"v1"'
        return Response(304, headers={"etag": '"v1"'})

    rec = mock_upstream(handler)
    r1 = await client.get("/simple/demo/", headers={"Accept": "text/html"})
    assert r1.status_code == 200
    assert r1.headers.get("x-cache") == "MISS"

    # Expire primary key; keep stale + etag. Also drop opposite warm cache so we
    # exercise the upstream 304 path instead of HIT-synthesized.
    cache._mem.pop("simple:demo:html", None)
    cache._mem.pop("simple:demo:json", None)
    assert await cache.get("simple:demo:html:stale") is not None

    r2 = await client.get("/simple/demo/", headers={"Accept": "text/html"})
    assert r2.status_code == 200
    assert r2.headers.get("x-cache") == "REVALIDATED"
    assert "a.whl" in r2.text
    assert rec.call_count == 2


async def test_completed_metadata_stays_cacheable_after_304(client, mock_upstream, monkeypatch):
    monkeypatch.setattr(settings, "enable_background_metadata", True)
    enriched_html = HTML.replace(">a.whl</a>", ' data-core-metadata="true">a.whl</a>')
    state = {"n": 0}

    def handler(url, headers=None):
        state["n"] += 1
        if state["n"] == 1:
            return Response(
                200,
                text=enriched_html,
                headers={"content-type": "text/html", "etag": '"v1"'},
            )
        assert (headers or {}).get("If-None-Match") == '"v1"'
        return Response(304, headers={"etag": '"v1"'})

    mock_upstream(handler)
    first = await client.get("/simple/demo/", headers={"Accept": "text/html"})
    assert first.headers["cache-control"] == "public, max-age=1"
    await drain_metadata_tasks()

    ready = await client.get("/simple/demo/", headers={"Accept": "text/html"})
    assert ready.headers["cache-control"].startswith("public, max-age=")
    assert int(ready.headers["cache-control"].rsplit("=", 1)[1]) > 1
    cache._mem.pop("simple:demo:html", None)
    cache._mem.pop("simple:demo:json", None)

    revalidated = await client.get("/simple/demo/", headers={"Accept": "text/html"})
    assert revalidated.headers["x-cache"] == "REVALIDATED"
    assert revalidated.headers["cache-control"].startswith("public, max-age=")
    assert int(revalidated.headers["cache-control"].rsplit("=", 1)[1]) > 1
    again = await client.get("/simple/demo/", headers={"Accept": "text/html"})
    assert again.headers["x-cache"] == "HIT"
    assert again.headers["cache-control"].startswith("public, max-age=")
    assert int(again.headers["cache-control"].rsplit("=", 1)[1]) > 1


async def test_revalidate_304_last_modified_only(client, mock_upstream):
    state = {"n": 0}
    lastmod = "Tue, 02 Jan 2024 12:00:00 GMT"

    def handler(url, headers=None):
        state["n"] += 1
        headers = headers or {}
        if state["n"] == 1:
            return Response(
                200,
                text=HTML,
                headers={"content-type": "text/html", "last-modified": lastmod},
            )
        assert headers.get("If-Modified-Since") == lastmod
        assert "If-None-Match" not in headers
        return Response(304, headers={"last-modified": lastmod})

    mock_upstream(handler)
    await client.get("/simple/demo/", headers={"Accept": "text/html"})
    cache._mem.pop("simple:demo:html", None)
    cache._mem.pop("simple:demo:json", None)
    r2 = await client.get("/simple/demo/", headers={"Accept": "text/html"})
    assert r2.status_code == 200
    assert r2.headers.get("x-cache") == "REVALIDATED"


async def test_orphan_etag_is_not_sent_and_does_not_502(client, mock_upstream):
    """An etag that outlived its body (LRU eviction) must not strand the project.

    Validators are small and bodies are large, so Redis under allkeys-lru evicts
    the body first. Revalidating against a body we cannot serve used to 502 for
    the remainder of the etag TTL.
    """
    seen: list[dict] = []

    def handler(url, headers=None):
        seen.append(dict(headers or {}))
        return Response(200, text=HTML, headers={"content-type": "text/html"})

    mock_upstream(handler)
    # Seed etag only — no stale body
    await cache.setex("simple:demo:etag", 600, '"x"')
    r = await client.get("/simple/demo/", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert "a.whl" in r.text
    # the orphaned validator must not have been sent
    assert "If-None-Match" not in seen[0]


async def test_304_with_vanished_stale_refetches_unconditionally(client, mock_upstream):
    """Stale body lost between the pre-flight check and the 304 -> refetch, not 502."""
    state = {"n": 0}

    def handler(url, headers=None):
        state["n"] += 1
        if state["n"] == 1:
            # validator was sent because a stale body existed at request time
            assert (headers or {}).get("If-None-Match") == '"x"'
            return Response(304, headers={"etag": '"x"'})
        # unconditional refetch after the stale body turned out to be gone
        assert "If-None-Match" not in (headers or {})
        return Response(200, text=HTML, headers={"content-type": "text/html"})

    rec = mock_upstream(handler)
    await cache.setex("simple:demo:etag", 600, '"x"')
    await cache.setex("simple:demo:html:stale", 600, HTML)

    # Drop the stale body after the have_stale pre-flight has already passed.
    orig_get = cache.get

    async def vanishing_get(key: str):
        value = await orig_get(key)
        if key == "simple:demo:html:stale":
            cache._mem.pop(key, None)
        return value

    cache.get = vanishing_get  # ty: ignore[invalid-assignment]
    try:
        r = await client.get("/simple/demo/", headers={"Accept": "text/html"})
    finally:
        cache.get = orig_get  # ty: ignore[invalid-assignment]

    assert r.status_code == 200
    assert "a.whl" in r.text
    assert rec.call_count == 2


async def test_304_with_no_stale_anywhere_still_502s(client, mock_upstream):
    """If upstream 304s even without validators there is genuinely nothing to serve."""
    mock_upstream(lambda url, headers=None: Response(304, headers={"etag": '"x"'}))
    await cache.setex("simple:demo:etag", 600, '"x"')
    await cache.setex("simple:demo:html:stale", 600, HTML)

    orig_get = cache.get

    async def vanishing_get(key: str):
        value = await orig_get(key)
        if key == "simple:demo:html:stale":
            cache._mem.pop(key, None)
        return value

    cache.get = vanishing_get  # ty: ignore[invalid-assignment]
    try:
        r = await client.get("/simple/demo/", headers={"Accept": "text/html"})
    finally:
        cache.get = orig_get  # ty: ignore[invalid-assignment]
    assert r.status_code == 502


async def test_304_synthesizes_from_opposite_stale(client, mock_upstream):
    await cache.setex("simple:demo:html:stale", 600, HTML)
    await cache.setex("simple:demo:etag", 600, '"v1"')

    def handler(url, headers=None):
        assert (headers or {}).get("If-None-Match") == '"v1"'
        return Response(304, headers={"etag": '"v1"'})

    mock_upstream(handler)
    r = await client.get("/simple/demo/", headers={"Accept": "application/vnd.pypi.simple.v1+json"})
    assert r.status_code == 200
    assert r.headers.get("x-cache") == "REVALIDATED"
    data = r.json()
    assert data["name"] == "demo"
    assert data["files"][0]["hashes"]["sha256"] == "abc"


async def test_cache_control_shortens_setex_ttl(client, mock_upstream, monkeypatch):
    recorded: list[tuple[str, int]] = []
    orig = cache.setex

    async def spy(key: str, ttl: int, value: str):
        recorded.append((key, ttl))
        return await orig(key, ttl, value)

    monkeypatch.setattr(cache, "setex", spy)
    mock_upstream(
        lambda url, headers=None: Response(
            200,
            text=HTML,
            headers={"content-type": "text/html", "cache-control": "max-age=5"},
        )
    )
    await client.get("/simple/demo/", headers={"Accept": "text/html"})
    primary = [ttl for key, ttl in recorded if key == "simple:demo:html"]
    assert primary
    assert primary[0] == 5
    assert primary[0] < settings.cache_project_ttl_seconds
