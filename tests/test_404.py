from httpx import Response

from app.config import settings


async def test_404_cached_stays_404(client, mock_upstream):
    rec = mock_upstream(
        lambda url, headers=None: Response(
            404, text="Not Found", headers={"content-type": "text/plain"}
        )
    )

    r1 = await client.get(
        "/simple/missing-pkg/", headers={"Accept": "application/vnd.pypi.simple.v1+json"}
    )
    assert r1.status_code == 404
    assert r1.headers.get("x-cache") == "MISS"
    assert rec.call_count == 1

    r2 = await client.get(
        "/simple/missing-pkg/", headers={"Accept": "application/vnd.pypi.simple.v1+json"}
    )
    assert r2.status_code == 404
    assert r2.headers.get("x-cache") == "HIT"
    assert rec.call_count == 1  # no second upstream fetch


async def test_metadata_microcache_does_not_override_404_policy(client, mock_upstream, monkeypatch):
    monkeypatch.setattr(settings, "enable_background_metadata", True)
    mock_upstream(lambda url, headers=None: Response(404, text="Not Found"))

    response = await client.get("/simple/missing-with-metadata/", headers={"Accept": "text/html"})

    assert response.status_code == 404
    assert "cache-control" not in response.headers


async def test_404_does_not_poison_200(client, mock_upstream):
    """A cached 404 must never be served as a 200 success body."""
    responses = [
        Response(404, text="Not Found"),
        Response(
            200,
            text='<!DOCTYPE html><html><body><a href="https://files.pythonhosted.org/p.whl#sha256=abc">p.whl</a></body></html>',
            headers={"content-type": "text/html"},
        ),
    ]
    state = {"i": 0}

    def handler(url, headers=None):
        i = state["i"]
        state["i"] = min(i + 1, len(responses) - 1)
        return responses[i]

    rec = mock_upstream(handler)

    r1 = await client.get("/simple/later-published/", headers={"Accept": "text/html"})
    assert r1.status_code == 404
    assert rec.call_count == 1

    # While 404 is cached, must still be 404 — never 200 with the 404 body
    r2 = await client.get("/simple/later-published/", headers={"Accept": "text/html"})
    assert r2.status_code == 404
    assert r2.headers.get("x-cache") == "HIT"
    assert "p.whl" not in r2.text
    assert rec.call_count == 1

    # Expire negative cache and confirm a real 200 can be stored afterwards
    from app.cache import cache

    cache._mem.pop("simple:later-published:404", None)
    r3 = await client.get("/simple/later-published/", headers={"Accept": "text/html"})
    assert r3.status_code == 200
    assert "p.whl" in r3.text
    assert r3.headers.get("x-cache") == "MISS"
    assert rec.call_count == 2

    r4 = await client.get("/simple/later-published/", headers={"Accept": "text/html"})
    assert r4.status_code == 200
    assert "HIT" in r4.headers.get("x-cache", "")
    assert rec.call_count == 2
