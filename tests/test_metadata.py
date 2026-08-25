from __future__ import annotations

from httpx import Response

from app.cache import cache
from app.config import settings
from app.metadata import drain_metadata_tasks, metadata_head_url


def test_metadata_head_url_from_artifacts_path():
    assert (
        metadata_head_url("/artifacts/files.pythonhosted.org/packages/a.whl#sha256=abc")
        == "https://files.pythonhosted.org/packages/a.whl.metadata"
    )


def test_metadata_head_url_refuses_non_allowlisted_hosts():
    """A hostile index must not be able to aim our probes at arbitrary hosts.

    Off-allowlist links are deliberately left un-rewritten, so probing them
    would reintroduce exactly the outbound reach the allowlist exists to deny.
    """
    assert metadata_head_url("https://evil.example/pkg.whl#sha256=abc") is None
    assert metadata_head_url("/artifacts/evil.example/pkg.whl") is None
    # non-http schemes are rejected outright
    assert metadata_head_url("file:///etc/passwd") is None
    assert metadata_head_url("gopher://evil.example/x") is None


def test_metadata_head_url_honours_configured_scheme(monkeypatch):
    """An extra allowlisted host may be plain http; /artifacts/ lost the scheme."""
    monkeypatch.setattr(settings, "artifact_host_allowlist", "nexus.internal")
    monkeypatch.setattr(settings, "artifact_host_scheme", "http")
    assert (
        metadata_head_url("/artifacts/nexus.internal/repo/a.whl")
        == "http://nexus.internal/repo/a.whl.metadata"
    )
    # the primary files host keeps the scheme from upstream_files_url
    primary = metadata_head_url("/artifacts/files.pythonhosted.org/p/a.whl")
    assert primary is not None and primary.startswith("https://")


async def test_probe_skips_non_allowlisted_host(client, mock_upstream, monkeypatch):
    """End-to-end: an off-allowlist link must produce no HEAD at all."""
    monkeypatch.setattr(settings, "enable_background_metadata", True)
    html = (
        "<!DOCTYPE html><html><body>"
        '<a href="https://evil.example/pkg.whl#sha256=abc">pkg.whl</a>'
        "</body></html>"
    )
    rec = mock_upstream(
        lambda url, headers=None: Response(200, text=html, headers={"content-type": "text/html"})
    )
    r = await client.get(
        "/simple/hostile/", headers={"Accept": "application/vnd.pypi.simple.v1+json"}
    )
    assert r.status_code == 200
    await drain_metadata_tasks()
    assert [c for c in rec.calls if c["method"] == "HEAD"] == []


async def test_metadata_probe_off_by_default(client, mock_upstream, monkeypatch):
    monkeypatch.setattr(settings, "enable_background_metadata", False)
    html = (
        "<!DOCTYPE html><html><body>"
        '<a href="https://files.pythonhosted.org/packages/a.whl#sha256=abc">a.whl</a>'
        "</body></html>"
    )
    rec = mock_upstream(
        lambda url, headers=None: Response(200, text=html, headers={"content-type": "text/html"})
    )
    r = await client.get(
        "/simple/metaoff/", headers={"Accept": "application/vnd.pypi.simple.v1+json"}
    )
    assert r.status_code == 200
    assert r.json()["files"][0]["core-metadata"] is False
    await drain_metadata_tasks()
    assert all(c["method"] == "GET" for c in rec.calls)


async def test_metadata_probe_on_enriches_cache(client, mock_upstream, monkeypatch):
    monkeypatch.setattr(settings, "enable_background_metadata", True)
    html = (
        "<!DOCTYPE html><html><body>"
        '<a href="https://files.pythonhosted.org/packages/a.whl#sha256=abc">a.whl</a>'
        "</body></html>"
    )

    def get_handler(url, headers=None):
        return Response(200, text=html, headers={"content-type": "text/html"})

    def head_handler(url, headers=None):
        assert str(url).endswith(".metadata")
        return Response(200)

    rec = mock_upstream(get_handler, head_handler=head_handler)
    r = await client.get(
        "/simple/metaon/", headers={"Accept": "application/vnd.pypi.simple.v1+json"}
    )
    assert r.status_code == 200
    # Hot path still advertises false until enrichment finishes
    assert r.json()["files"][0].get("core-metadata") is False

    await drain_metadata_tasks()
    assert any(c["method"] == "HEAD" for c in rec.calls)

    # Expire primary so we re-read enriched cache without HIT short-circuit from old body
    # Enrichment wrote both formats; clear mem then get from keys directly
    body = await cache.get("simple:metaon:json")
    assert body is not None
    import json

    data = json.loads(body)
    assert data["files"][0]["core-metadata"] is True


async def test_enrichment_reuses_response_ttl(client, mock_upstream, monkeypatch):
    """Enrichment must not extend a TTL we shortened to honour upstream max-age."""
    monkeypatch.setattr(settings, "enable_background_metadata", True)
    html = (
        "<!DOCTYPE html><html><body>"
        '<a href="https://files.pythonhosted.org/packages/a.whl#sha256=abc">a.whl</a>'
        "</body></html>"
    )
    recorded: list[tuple[str, int]] = []
    orig = cache.setex

    async def spy(key: str, ttl: int, value: str):
        recorded.append((key, ttl))
        return await orig(key, ttl, value)

    monkeypatch.setattr(cache, "setex", spy)
    mock_upstream(
        lambda url, headers=None: Response(
            200,
            text=html,
            headers={"content-type": "text/html", "cache-control": "max-age=7"},
        ),
        head_handler=lambda url, headers=None: Response(200),
    )
    await client.get("/simple/ttlcheck/", headers={"Accept": "text/html"})
    await drain_metadata_tasks()

    primary = [ttl for key, ttl in recorded if key == "simple:ttlcheck:html"]
    assert primary, "primary key was never written"
    # every write of the primary key uses the upstream-derived TTL, including
    # the one the background enrichment makes
    assert set(primary) == {7}


async def test_enrichment_does_not_clobber_a_newer_body(client, mock_upstream, monkeypatch):
    """A slow enrichment must not overwrite an entry a later fill already replaced."""
    monkeypatch.setattr(settings, "enable_background_metadata", True)
    from app.metadata import _enrich

    stale_source = "<html><body>OLD</body></html>"
    await cache.setex("simple:racy:html", 60, "<html><body>NEW</body></html>")
    # enrich from a body that is no longer what is cached
    await _enrich("racy", stale_source, is_json=False, ttl=60)
    assert await cache.get("simple:racy:html") == "<html><body>NEW</body></html>"


async def test_head_concurrency_is_globally_bounded(monkeypatch):
    """The cap is global, not per project: N projects must not give N*limit HEADs."""
    import asyncio

    import app.metadata as meta

    monkeypatch.setattr(settings, "enable_background_metadata", True)
    monkeypatch.setattr(meta, "_head_semaphore", asyncio.Semaphore(3))
    monkeypatch.setattr(meta, "_task_semaphore", asyncio.Semaphore(10))

    live = 0
    peak = 0

    class FakeClient:
        async def head(self, url):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1
            return Response(404)

    monkeypatch.setattr(meta, "get_http_client", lambda: FakeClient())

    html = (
        "<!DOCTYPE html><html><body>"
        + "".join(
            f'<a href="https://files.pythonhosted.org/packages/f{i}.whl#sha256=a{i}">f{i}.whl</a>'
            for i in range(8)
        )
        + "</body></html>"
    )

    # five projects enriching at once
    await asyncio.gather(*(meta._enrich(f"proj{i}", html, is_json=False, ttl=60) for i in range(5)))
    assert peak <= 3, f"peak concurrent HEADs was {peak}, expected <= 3"
