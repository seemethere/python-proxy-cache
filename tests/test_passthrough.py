from httpx import Response

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


async def test_passthrough_when_upstream_already_has_json(client, mock_upstream):
    """Upstream JSON + client JSON: URLs rewritten, every other field byte-identical."""
    mock_upstream(
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

    r2 = await client.get("/simple/requests/", headers={"Accept": "text/html"})
    assert r2.status_code == 200
    assert "requests-2.31.0" in r2.text
    assert "HIT" in r2.headers.get("x-cache", "")


async def test_passthrough_html_upstream_html_client(client, mock_upstream):
    html = (
        "<!DOCTYPE html><html><body>"
        '<a href="https://files.pythonhosted.org/packages/a.whl#sha256=abc">a.whl</a>'
        "</body></html>"
    )
    mock_upstream(
        lambda url, headers=None: Response(200, text=html, headers={"content-type": "text/html"})
    )

    r = await client.get("/simple/requests/", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert r.headers.get("x-synthesis") == "0"
    assert "a.whl" in r.text
