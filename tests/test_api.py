import pytest
from httpx import ASGITransport, AsyncClient, Response

from app.cache import cache
from app.main import app

HTML_UPSTREAM = """<!DOCTYPE html><html><body>
<a href="https://files.pythonhosted.org/packages/a/requests-2.31.0-py3-none-any.whl#sha256=abc">requests-2.31.0-py3-none-any.whl</a>
</body></html>"""


@pytest.mark.asyncio
async def test_synthesis_html_upstream_serves_json():
    # mock the app's upstream client only, not the test client
    import httpx

    import app.main as m

    if m.http_client is None:
        m.http_client = httpx.AsyncClient()
    orig_get = m.http_client.get

    async def mock_get(url, headers=None):
        return Response(200, text=HTML_UPSTREAM, headers={"content-type": "text/html"})

    m.http_client.get = mock_get
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            cache._mem.clear()
            # first request wants JSON but upstream only has HTML -> synthesis
            r = await ac.get(
                "/simple/requests/", headers={"Accept": "application/vnd.pypi.simple.v1+json"}
            )
            assert r.status_code == 200
            assert "application/vnd.pypi.simple.v1+json" in r.headers["content-type"]
            data = r.json()
            assert data["name"] == "requests"
            assert data["files"][0]["hashes"]["sha256"] == "abc"
            assert r.headers["x-cache"] == "MISS" or r.headers.get("X-Cache") == "MISS"

            # second request should be HIT (or HIT-synthesized)
            r2 = await ac.get(
                "/simple/requests/", headers={"Accept": "application/vnd.pypi.simple.v1+json"}
            )
            assert "HIT" in r2.headers.get("x-cache", r2.headers.get("X-Cache", ""))

            # HTML request should be HIT-synthesized without upstream call
            r3 = await ac.get("/simple/requests/", headers={"Accept": "text/html"})
            assert r3.status_code == 200
            assert "requests-2.31.0" in r3.text
    finally:
        m.http_client.get = orig_get
        cache._mem.clear()
