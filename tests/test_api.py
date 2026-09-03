from httpx import Response
from starlette.responses import Response as StarletteResponse

from app.main import _vary_on_accept

HTML_UPSTREAM = """<!DOCTYPE html><html><body>
<a href="https://files.pythonhosted.org/packages/a/requests-2.31.0-py3-none-any.whl#sha256=abc">requests-2.31.0-py3-none-any.whl</a>
</body></html>"""


def test_vary_accept_preserves_existing_values_without_duplicates():
    response = StarletteResponse(headers={"Vary": "Origin, accept"})
    _vary_on_accept(response)
    assert response.headers["vary"] == "Origin, accept"


def test_vary_wildcard_is_not_modified():
    response = StarletteResponse(headers={"Vary": "*"})
    _vary_on_accept(response)
    assert response.headers["vary"] == "*"


async def test_synthesis_html_upstream_serves_json(client, mock_upstream):
    mock_upstream(
        lambda url, headers=None: Response(
            200, text=HTML_UPSTREAM, headers={"content-type": "text/html"}
        )
    )

    r = await client.get(
        "/simple/requests/", headers={"Accept": "application/vnd.pypi.simple.v1+json"}
    )
    assert r.status_code == 200
    assert "application/vnd.pypi.simple.v1+json" in r.headers["content-type"]
    data = r.json()
    assert data["name"] == "requests"
    assert data["files"][0]["hashes"]["sha256"] == "abc"
    assert r.headers.get("x-cache") == "MISS"

    r2 = await client.get(
        "/simple/requests/", headers={"Accept": "application/vnd.pypi.simple.v1+json"}
    )
    assert "HIT" in r2.headers.get("x-cache", "")

    r3 = await client.get("/simple/requests/", headers={"Accept": "text/html"})
    assert r3.status_code == 200
    assert "requests-2.31.0" in r3.text
