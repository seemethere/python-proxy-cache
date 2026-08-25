import pytest
from httpx import ASGITransport, AsyncClient, Response

from app.cache import cache
from app.main import app

JSON_UPSTREAM = {
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


@pytest.mark.asyncio
async def test_passthrough_when_upstream_already_has_json():
    """Upstream already compliant JSON -> client wants JSON = no synthesis, verbatim body."""
    import httpx

    import app.main as m

    if m.http_client is None:
        m.http_client = httpx.AsyncClient()
    orig_get = m.http_client.get

    async def mock_get(url, headers=None):
        return Response(
            200, json=JSON_UPSTREAM, headers={"content-type": "application/vnd.pypi.simple.v1+json"}
        )

    m.http_client.get = mock_get  # ty: ignore[invalid-assignment]
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            cache._mem.clear()
            r = await ac.get(
                "/simple/requests/", headers={"Accept": "application/vnd.pypi.simple.v1+json"}
            )
            assert r.status_code == 200
            assert r.headers.get("x-synthesis") == "0" or r.headers.get("X-Synthesis") == "0"
            assert r.json() == JSON_UPSTREAM
            # opposite format should now be cached synthesized
            r2 = await ac.get("/simple/requests/", headers={"Accept": "text/html"})
            assert r2.status_code == 200
            assert "requests-2.31.0" in r2.text
            assert "HIT" in r2.headers.get("x-cache", r2.headers.get("X-Cache", ""))
    finally:
        m.http_client.get = orig_get  # ty: ignore[invalid-assignment]
        cache._mem.clear()


@pytest.mark.asyncio
async def test_passthrough_html_upstream_html_client():
    html = '<!DOCTYPE html><html><body><a href="https://files.pythonhosted.org/packages/a.whl#sha256=abc">a.whl</a></body></html>'
    import httpx

    import app.main as m

    if m.http_client is None:
        m.http_client = httpx.AsyncClient()
    orig_get = m.http_client.get

    async def mock_get(url, headers=None):
        return Response(200, text=html, headers={"content-type": "text/html"})

    m.http_client.get = mock_get  # ty: ignore[invalid-assignment]
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            cache._mem.clear()
            r = await ac.get("/simple/requests/", headers={"Accept": "text/html"})
            assert r.status_code == 200
            assert r.headers.get("x-synthesis") == "0" or r.headers.get("X-Synthesis") == "0"
            assert "a.whl" in r.text
    finally:
        m.http_client.get = orig_get  # ty: ignore[invalid-assignment]
        cache._mem.clear()
