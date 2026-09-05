import json

from httpx import Response

from app.artifacts import host_allowed, rewrite_file_url, rewrite_simple_body
from app.config import settings


def test_rewrite_allowlisted_absolute():
    url = "https://files.pythonhosted.org/packages/a/b/c.whl#sha256=abc"
    out = rewrite_file_url(url)
    assert out.startswith("/artifacts/files.pythonhosted.org/packages/")
    assert out.endswith("#sha256=abc")


def test_rewrite_skips_non_allowlisted():
    url = "https://evil.example/pkg.whl#sha256=abc"
    assert rewrite_file_url(url) == url


def test_rewrite_idempotent():
    url = "/artifacts/files.pythonhosted.org/packages/x.whl#sha256=abc"
    assert rewrite_file_url(url) == url


def test_rewrite_relative_against_files_base():
    out = rewrite_file_url("../../packages/a.whl#sha256=abc")
    assert out.startswith("/artifacts/files.pythonhosted.org/")
    assert "sha256=abc" in out


def test_host_allowed_from_settings():
    assert host_allowed("files.pythonhosted.org")
    assert not host_allowed("evil.example")


def test_rewrite_simple_json_body():
    body = """{
      "name": "demo",
      "files": [{
        "filename": "a.whl",
        "url": "https://files.pythonhosted.org/packages/a.whl",
        "hashes": {"sha256": "abc"}
      }],
      "meta": {"api-version": "1.1"}
    }"""
    out = rewrite_simple_body(body, is_json=True, project_name="demo")
    data = json.loads(out)
    assert data["files"][0]["url"].startswith("/artifacts/files.pythonhosted.org/")


def test_disabled_rewrite_keeps_plain_json_urls_direct(monkeypatch):
    monkeypatch.setattr(settings, "rewrite_artifact_urls", False)
    body = json.dumps(
        {
            "files": [
                {"url": "https://files.pythonhosted.org/packages/plain.whl"},
                {
                    "url": "https://files.pythonhosted.org/packages/metadata.whl",
                    "core-metadata": True,
                },
            ]
        }
    )

    files = json.loads(rewrite_simple_body(body, is_json=True))["files"]

    assert files[0]["url"] == "https://files.pythonhosted.org/packages/plain.whl"
    assert files[1]["url"] == "/artifacts/files.pythonhosted.org/packages/metadata.whl"


def test_disabled_rewrite_plain_json_is_returned_verbatim(monkeypatch):
    monkeypatch.setattr(settings, "rewrite_artifact_urls", False)
    body = '{ "files": [{"url": "https://files.pythonhosted.org/plain.whl"}] }'

    assert rewrite_simple_body(body, is_json=True) is body


def test_disabled_rewrite_keeps_plain_html_urls_direct(monkeypatch):
    monkeypatch.setattr(settings, "rewrite_artifact_urls", False)
    body = (
        '<a href="https://files.pythonhosted.org/packages/plain.whl">plain</a>'
        '<a href="https://files.pythonhosted.org/packages/metadata.whl" '
        'data-dist-info-metadata="false" data-core-metadata="true">metadata</a>'
    )

    rewritten = rewrite_simple_body(body, is_json=False)

    assert 'href="https://files.pythonhosted.org/packages/plain.whl"' in rewritten
    assert 'href="/artifacts/files.pythonhosted.org/packages/metadata.whl"' in rewritten


def test_disabled_rewrite_plain_html_is_returned_verbatim(monkeypatch):
    monkeypatch.setattr(settings, "rewrite_artifact_urls", False)
    body = '<A HREF="https://files.pythonhosted.org/plain.whl">plain</A>'

    assert rewrite_simple_body(body, is_json=False) is body


def test_extra_allowlist(monkeypatch):
    monkeypatch.setattr(
        settings,
        "artifact_host_allowlist",
        "nexus.example.com",
    )
    # rebuild host set via method
    assert "nexus.example.com" in settings.artifact_hosts()
    url = "https://nexus.example.com/repo/a.whl"
    assert rewrite_file_url(url).startswith("/artifacts/nexus.example.com/")


async def test_project_response_rewrites_urls(client, mock_upstream):
    from httpx import Response

    mock_upstream(
        lambda url, headers=None: Response(
            200,
            text=(
                "<!DOCTYPE html><html><body>"
                '<a href="https://files.pythonhosted.org/packages/a.whl#sha256=abc">a.whl</a>'
                "</body></html>"
            ),
            headers={"content-type": "text/html"},
        )
    )
    r = await client.get("/simple/demo/", headers={"Accept": "application/vnd.pypi.simple.v1+json"})
    assert r.status_code == 200
    data = r.json()
    assert data["files"][0]["url"].startswith("/artifacts/files.pythonhosted.org/")


PEP_691_FULL = json.dumps(
    {
        "name": "demo",
        # PEP 700
        "versions": ["1.0", "2.0"],
        "files": [
            {
                "filename": "a.whl",
                "url": "https://files.pythonhosted.org/packages/a.whl",
                "hashes": {"sha256": "abc"},
                "core-metadata": {"sha256": "deadbeef"},
                "size": 1234,
                "upload-time": "2024-01-01T00:00:00Z",
                # PEP 740
                "provenance": "https://files.pythonhosted.org/a.whl.provenance",
            }
        ],
        # PEP 708
        "meta": {"api-version": "1.1", "tracks": ["https://example/simple/demo/"]},
        "alternate-locations": ["https://mirror/simple/demo/"],
    }
)


def test_json_rewrite_preserves_every_other_field():
    """URL rewriting must not become a lossy model round trip."""
    out = json.loads(rewrite_simple_body(PEP_691_FULL, is_json=True, project_name="demo"))
    original = json.loads(PEP_691_FULL)

    assert out["files"][0]["url"] == "/artifacts/files.pythonhosted.org/packages/a.whl"
    # everything else is byte-identical
    out["files"][0]["url"] = original["files"][0]["url"]
    assert out == original


def test_html_rewrite_preserves_surrounding_markup():
    html = (
        "<!DOCTYPE html>\n<html><head>"
        '<meta name="pypi:repository-version" content="1.3">'
        "</head><body>\n"
        '<a href="https://files.pythonhosted.org/packages/a.whl#sha256=abc" '
        'data-requires-python="&gt;=3.9" data-provenance="https://x/a.provenance">a.whl</a><br/>\n'
        '<a href="https://other.example/b.whl#sha256=def">b.whl</a>\n'
        "</body></html>"
    )
    out = rewrite_simple_body(html, is_json=False, project_name="demo")

    # allowlisted host rewritten, everything else untouched
    assert '<a href="/artifacts/files.pythonhosted.org/packages/a.whl#sha256=abc"' in out
    assert 'data-provenance="https://x/a.provenance"' in out
    assert 'content="1.3"' in out
    assert 'data-requires-python="&gt;=3.9"' in out
    # non-allowlisted host left alone
    assert '<a href="https://other.example/b.whl#sha256=def">' in out


async def test_passthrough_json_keeps_pep700_708_740_fields(client, mock_upstream):
    """End-to-end: the passthrough path must not strip unmodelled fields."""
    mock_upstream(
        lambda url, headers=None: Response(
            200,
            text=PEP_691_FULL,
            headers={"content-type": "application/vnd.pypi.simple.v1+json"},
        )
    )
    r = await client.get("/simple/demo/", headers={"Accept": "application/vnd.pypi.simple.v1+json"})
    assert r.status_code == 200
    assert r.headers.get("x-synthesis") == "0"
    data = r.json()
    assert data["versions"] == ["1.0", "2.0"]
    assert data["meta"]["tracks"] == ["https://example/simple/demo/"]
    assert data["alternate-locations"] == ["https://mirror/simple/demo/"]
    assert data["files"][0]["provenance"] == "https://files.pythonhosted.org/a.whl.provenance"
    assert data["files"][0]["core-metadata"] == {"sha256": "deadbeef"}
    assert data["files"][0]["url"].startswith("/artifacts/files.pythonhosted.org/")


async def test_disabled_rewrite_passthrough_json_leaves_plain_url_direct(
    client, mock_upstream, monkeypatch
):
    monkeypatch.setattr(settings, "rewrite_artifact_urls", False)
    body = json.loads(PEP_691_FULL)
    body["files"][0].pop("core-metadata")
    direct_url = body["files"][0]["url"]
    mock_upstream(
        lambda url, headers=None: Response(
            200,
            json=body,
            headers={"content-type": "application/vnd.pypi.simple.v1+json"},
        )
    )

    response = await client.get(
        "/simple/demo/", headers={"Accept": "application/vnd.pypi.simple.v1+json"}
    )

    assert response.headers["x-synthesis"] == "0"
    assert response.json()["files"][0]["url"] == direct_url


async def test_disabled_rewrite_passthrough_html_leaves_plain_url_direct(
    client, mock_upstream, monkeypatch
):
    monkeypatch.setattr(settings, "rewrite_artifact_urls", False)
    direct_url = "https://files.pythonhosted.org/packages/plain.whl"
    body = f'<html><body><a href="{direct_url}">plain.whl</a></body></html>'
    mock_upstream(
        lambda url, headers=None: Response(200, text=body, headers={"content-type": "text/html"})
    )

    response = await client.get("/simple/demo/", headers={"Accept": "text/html"})

    assert response.headers["x-synthesis"] == "0"
    assert direct_url in response.text


def test_rewrite_preserves_non_default_port(monkeypatch):
    """urlparse().hostname drops the port; an internal index on :8081 must survive."""
    monkeypatch.setattr(settings, "artifact_host_allowlist", "nexus.internal:8081")
    out = rewrite_file_url("http://nexus.internal:8081/repo/a.whl#sha256=abc")
    assert out == "/artifacts/nexus.internal:8081/repo/a.whl#sha256=abc"


def test_default_port_is_omitted(monkeypatch):
    monkeypatch.setattr(settings, "artifact_host_allowlist", "")
    assert rewrite_file_url("https://files.pythonhosted.org:443/p/a.whl") == (
        "/artifacts/files.pythonhosted.org/p/a.whl"
    )


def test_allowlist_is_port_strict(monkeypatch):
    """A bare host entry must not authorise arbitrary ports on that host."""
    monkeypatch.setattr(settings, "artifact_host_allowlist", "nexus.internal")
    assert host_allowed("nexus.internal")
    assert not host_allowed("nexus.internal:1337")
    # an explicit host:port entry authorises only that port
    monkeypatch.setattr(settings, "artifact_host_allowlist", "nexus.internal:8081")
    assert host_allowed("nexus.internal:8081")
    assert not host_allowed("nexus.internal:9999")
    url = "http://nexus.internal:1337/x.whl"
    assert rewrite_file_url(url) == url  # left alone, not rewritten
