from __future__ import annotations

from httpx import Response

from app.cache import cache
from app.config import settings
from app.metadata import (
    _artifact_upstream_url,
    _extraction_candidate_indexes,
    _pending,
    _probe,
    _safe_url_for_log,
    drain_metadata_tasks,
    load_metadata_for_url,
    metadata_head_url,
    schedule_metadata_enrichment,
    store_extracted_metadata,
)
from app.metrics import metrics
from app.models import File


def test_metadata_logs_redact_credentials_and_signed_query():
    assert (
        _safe_url_for_log("https://user:secret@example.test:8443/packages/a.whl?signature=secret#x")
        == "https://example.test:8443/packages/a.whl"
    )


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


def test_metadata_head_url_preserves_signed_query_before_suffix():
    assert (
        metadata_head_url(
            "/artifacts/files.pythonhosted.org/packages/a.whl?signature=secret#sha256=abc"
        )
        == "https://files.pythonhosted.org/packages/a.whl.metadata?signature=secret"
    )
    assert (
        metadata_head_url(
            "https://files.pythonhosted.org/packages/a.whl?signature=secret#sha256=abc"
        )
        == "https://files.pythonhosted.org/packages/a.whl.metadata?signature=secret"
    )


def test_extraction_url_preserves_signed_query(monkeypatch):
    artifact = "/artifacts/files.pythonhosted.org/packages/a.whl?signature=secret#sha256=abc"
    assert (
        _artifact_upstream_url(artifact)
        == "https://files.pythonhosted.org/packages/a.whl?signature=secret"
    )
    monkeypatch.setattr(settings, "metadata_artifact_base_url", "http://nginx:8080")
    assert (
        _artifact_upstream_url(artifact)
        == "http://nginx:8080/artifacts/files.pythonhosted.org/packages/a.whl?signature=secret"
    )


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
        f'<a href="https://files.pythonhosted.org/packages/a-1.0-py3-none-any.whl'
        f'#sha256={"a" * 64}">a-1.0-py3-none-any.whl</a>'
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
    assert r.headers["cache-control"] == "no-store"

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
    monkeypatch.setattr(meta, "_probe_semaphore", asyncio.Semaphore(3))
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


async def test_probe_concurrency_bounds_cache_lookups(monkeypatch):
    import asyncio

    import app.metadata as meta

    monkeypatch.setattr(meta, "_probe_semaphore", asyncio.Semaphore(3))
    live = 0
    peak = 0

    async def delayed_cache_get(key: str) -> None:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1
        return None

    class FakeClient:
        async def head(self, url):
            return Response(200)

    monkeypatch.setattr(cache, "get", delayed_cache_get)
    monkeypatch.setattr(meta, "get_http_client", lambda: FakeClient())
    files = [
        File(
            filename=f"demo-{index}.0-py3-none-any.whl",
            url=f"/artifacts/files.pythonhosted.org/packages/demo-{index}.whl",
            hashes={"sha256": f"{index:064x}"},
        )
        for index in range(12)
    ]

    await asyncio.gather(*(meta._probe(file) for file in files))

    assert peak <= 3, f"peak concurrent cache lookups was {peak}, expected <= 3"


def test_metadata_probe_respects_port_in_authority(monkeypatch):
    """Probes must target the same authority the rewrite emitted, port included."""
    monkeypatch.setattr(settings, "artifact_host_allowlist", "nexus.internal:8081")
    monkeypatch.setattr(settings, "artifact_host_scheme", "http")
    assert (
        metadata_head_url("/artifacts/nexus.internal:8081/repo/a.whl")
        == "http://nexus.internal:8081/repo/a.whl.metadata"
    )
    # a different port on the same host is not authorised
    assert metadata_head_url("/artifacts/nexus.internal:9999/repo/a.whl") is None
    assert metadata_head_url("http://nexus.internal:9999/repo/a.whl") is None


async def test_extracted_metadata_is_content_addressed_and_served(client):
    import hashlib

    wheel_sha256 = "a" * 64
    content = b"Metadata-Version: 2.4\nName: demo\nVersion: 1.0\n"
    metadata_sha256 = hashlib.sha256(content).hexdigest()
    artifact_url = "/artifacts/files.pythonhosted.org/packages/demo.whl"

    advertised = await store_extracted_metadata(artifact_url, wheel_sha256, content)
    assert advertised == {"sha256": metadata_sha256}
    assert await load_metadata_for_url(artifact_url) == (content.decode(), metadata_sha256)

    response = await client.get(f"{artifact_url}.metadata")
    assert response.status_code == 200
    assert response.content == content
    assert response.headers["etag"] == f'"sha256:{metadata_sha256}"'
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


async def test_metadata_url_lookup_deliberately_ignores_signed_query(client):
    artifact_url = "/artifacts/files.pythonhosted.org/packages/demo.whl?signature=temporary"
    content = b"Metadata-Version: 2.4\nName: demo\nVersion: 1.0\n"
    await store_extracted_metadata(artifact_url, "a" * 64, content)

    assert (
        await load_metadata_for_url(
            "/artifacts/files.pythonhosted.org/packages/demo.whl?signature=renewed"
        )
        is not None
    )
    response = await client.get(
        "/artifacts/files.pythonhosted.org/packages/demo.whl.metadata?signature=renewed"
    )
    assert response.status_code == 200
    assert response.content == content


async def test_metadata_route_fails_closed(client):
    assert (
        await client.get("/artifacts/files.pythonhosted.org/packages/missing.whl.metadata")
    ).status_code == 404
    assert (
        await client.get("/artifacts/evil.example/packages/demo.whl.metadata")
    ).status_code == 404
    assert (
        await client.get("/artifacts/files.pythonhosted.org/packages/demo.tar.gz.metadata")
    ).status_code == 404


async def test_failed_head_extracts_and_advertises_hashed_metadata(
    client, mock_upstream, monkeypatch
):
    import hashlib
    import json

    import app.metadata as meta

    monkeypatch.setattr(settings, "enable_background_metadata", True)
    monkeypatch.setattr(settings, "metadata_artifact_base_url", "http://nginx:8080")
    wheel_sha256 = "b" * 64
    content = b"Metadata-Version: 2.4\nName: demo\nVersion: 1.0\n"
    metadata_sha256 = hashlib.sha256(content).hexdigest()
    upstream = json.dumps(
        {
            "name": "demo",
            "versions": ["1.0"],
            "files": [
                {
                    "filename": "demo-1.0-py3-none-any.whl",
                    "url": "https://files.pythonhosted.org/packages/demo.whl",
                    "hashes": {"sha256": wheel_sha256},
                    "provenance": "https://example.test/attestation",
                }
            ],
            "meta": {"api-version": "1.3", "tracks": ["https://example.test/simple/demo/"]},
        }
    )
    mock_upstream(
        lambda url, headers=None: Response(
            200,
            text=upstream,
            headers={"content-type": "application/vnd.pypi.simple.v1+json"},
        ),
        head_handler=lambda url, headers=None: Response(404),
    )
    extracted_urls: list[str] = []

    async def fake_extract(url: str) -> bytes:
        extracted_urls.append(url)
        return content

    monkeypatch.setattr(meta, "_extract_wheel_metadata", fake_extract)

    response = await client.get(
        "/simple/demo/", headers={"Accept": "application/vnd.pypi.simple.v1+json"}
    )
    assert response.status_code == 200
    assert "core-metadata" not in response.json()["files"][0]
    await drain_metadata_tasks()

    assert extracted_urls == [
        "http://nginx:8080/artifacts/files.pythonhosted.org/packages/demo.whl"
    ]
    body = await cache.get("simple:demo:json")
    assert body is not None
    enriched = json.loads(body)
    assert enriched["files"][0]["core-metadata"] == {"sha256": metadata_sha256}
    # Enrichment mutates only the advertisement and retains fields unknown to our model.
    assert enriched["versions"] == ["1.0"]
    assert enriched["meta"]["tracks"] == ["https://example.test/simple/demo/"]
    assert enriched["files"][0]["provenance"] == "https://example.test/attestation"

    metadata_response = await client.get(
        "/artifacts/files.pythonhosted.org/packages/demo.whl.metadata"
    )
    assert metadata_response.status_code == 200
    assert metadata_response.content == content


async def test_same_declared_wheel_sha_is_scoped_to_artifact_url(monkeypatch):
    import app.metadata as meta

    wheel_sha256 = "c" * 64
    calls: list[str] = []

    class FakeClient:
        async def head(self, url):
            return Response(404)

    async def fake_extract(url: str) -> bytes:
        calls.append(url)
        version = "1.0" if "/one/" in url else "2.0"
        return f"Metadata-Version: 2.4\nName: demo\nVersion: {version}\n".encode()

    monkeypatch.setattr(meta, "get_http_client", lambda: FakeClient())
    monkeypatch.setattr(meta, "_extract_wheel_metadata", fake_extract)
    first = File(
        filename="demo.whl",
        url="/artifacts/files.pythonhosted.org/one/demo.whl",
        hashes={"sha256": wheel_sha256},
    )
    second = File(
        filename="demo.whl",
        url="/artifacts/files.pythonhosted.org/two/demo.whl",
        hashes={"sha256": wheel_sha256},
    )

    import asyncio

    first_result, second_result = await asyncio.gather(_probe(first), _probe(second))

    assert set(calls) == {
        "https://files.pythonhosted.org/one/demo.whl",
        "https://files.pythonhosted.org/two/demo.whl",
    }
    assert first_result.core_metadata != second_result.core_metadata
    first_stored = await load_metadata_for_url(first.url)
    second_stored = await load_metadata_for_url(second.url)
    assert first_stored is not None and "Version: 1.0" in first_stored[0]
    assert second_stored is not None and "Version: 2.0" in second_stored[0]


async def test_enrichment_schedule_has_hard_pending_bound(monkeypatch):
    import asyncio

    import app.metadata as meta

    await drain_metadata_tasks()
    monkeypatch.setattr(settings, "enable_background_metadata", True)
    monkeypatch.setattr(settings, "metadata_max_pending_projects", 2)
    release = asyncio.Event()

    async def blocked_enrich(canonical: str, body: str, *, is_json: bool, ttl: int) -> None:
        await release.wait()

    monkeypatch.setattr(meta, "_enrich", blocked_enrich)
    dropped_before = metrics["metadata_enrichment_dropped"]

    schedule_metadata_enrichment("one", "", is_json=False, ttl=60)
    schedule_metadata_enrichment("two", "", is_json=False, ttl=60)
    schedule_metadata_enrichment("three", "", is_json=False, ttl=60)

    assert len(_pending) == 2
    assert metrics["metadata_enrichment_dropped"] == dropped_before + 1
    release.set()
    await drain_metadata_tasks()


async def test_enrichment_schedule_coalesces_canonical_project(monkeypatch):
    import asyncio

    import app.metadata as meta

    await drain_metadata_tasks()
    monkeypatch.setattr(settings, "enable_background_metadata", True)
    monkeypatch.setattr(settings, "metadata_max_pending_projects", 1)
    release = asyncio.Event()
    calls: list[str] = []

    async def blocked_enrich(canonical: str, body: str, *, is_json: bool, ttl: int) -> None:
        calls.append(canonical)
        await release.wait()

    monkeypatch.setattr(meta, "_enrich", blocked_enrich)
    dropped_before = metrics["metadata_enrichment_dropped"]

    schedule_metadata_enrichment("Example_Pkg", "first", is_json=False, ttl=60)
    schedule_metadata_enrichment("example-pkg", "second", is_json=True, ttl=30)
    await asyncio.sleep(0)

    assert calls == ["example-pkg"]
    assert len(_pending) == 1
    assert metrics["metadata_enrichment_dropped"] == dropped_before

    release.set()
    await drain_metadata_tasks()
    schedule_metadata_enrichment("example.pkg", "third", is_json=False, ttl=60)
    await drain_metadata_tasks()

    assert calls == ["example-pkg", "example-pkg"]


def test_extraction_candidates_prefer_newest_valid_wheels(monkeypatch):
    monkeypatch.setattr(settings, "metadata_max_extract_files_per_project", 3)
    files = [
        File(filename="demo-1.0-py3-none-any.whl", url="/old"),
        File(filename="not-a-valid-wheel.whl", url="/invalid"),
        File(filename="demo-4.0.tar.gz", url="/sdist"),
        File(filename="demo-3.0-py3-none-any.whl", url="/new-a"),
        File(filename="demo-2.0-py3-none-any.whl", url="/middle"),
        File(filename="demo-3.0-py3-none-manylinux_2_17_x86_64.whl", url="/new-b"),
        File(filename="demo-4.0-py3-none-any.whl", url="/present", core_metadata=True),
    ]

    assert _extraction_candidate_indexes(files) == {3, 4, 5}


async def test_large_project_only_probes_bounded_candidates_and_publishes(
    client, mock_upstream, monkeypatch
):
    import app.metadata as meta

    monkeypatch.setattr(settings, "enable_background_metadata", True)
    monkeypatch.setattr(settings, "metadata_max_extract_files_per_project", 3)
    upstream_metadata_hash = "f" * 64
    html = (
        "<!DOCTYPE html><html><body>"
        + "".join(
            (
                f'<a href="https://files.pythonhosted.org/packages/demo-{version}.0-py3-none-any.whl'
                f'#sha256={version:064x}">demo-{version}.0-py3-none-any.whl</a>'
            )
            for version in range(1000)
        )
        + (
            '<a href="https://files.pythonhosted.org/packages/demo-1001.0-py3-none-any.whl'
            f'#sha256={1001:064x}" data-core-metadata="sha256={upstream_metadata_hash}">'
            "demo-1001.0-py3-none-any.whl</a>"
        )
        + "</body></html>"
    )
    recorder = mock_upstream(
        lambda url, headers=None: Response(200, text=html, headers={"content-type": "text/html"}),
        head_handler=lambda url, headers=None: Response(404),
    )
    extracted_urls: list[str] = []

    async def fake_extract(url: str) -> bytes:
        extracted_urls.append(url)
        return b"Metadata-Version: 2.4\nName: demo\nVersion: 1.0\n"

    monkeypatch.setattr(meta, "_extract_wheel_metadata", fake_extract)
    response = await client.get(
        "/simple/bounded/", headers={"Accept": "application/vnd.pypi.simple.v1+json"}
    )
    assert response.status_code == 200
    await drain_metadata_tasks()

    assert len([call for call in recorder.calls if call["method"] == "HEAD"]) == 3
    assert len(extracted_urls) == 3
    assert {url.rsplit("/", 1)[-1] for url in extracted_urls} == {
        "demo-997.0-py3-none-any.whl",
        "demo-998.0-py3-none-any.whl",
        "demo-999.0-py3-none-any.whl",
    }
    enriched = await client.get(
        "/simple/bounded/", headers={"Accept": "application/vnd.pypi.simple.v1+json"}
    )
    files_by_name = {file["filename"]: file for file in enriched.json()["files"]}
    for version in (997, 998, 999):
        assert isinstance(
            files_by_name[f"demo-{version}.0-py3-none-any.whl"]["core-metadata"], dict
        )
    assert files_by_name["demo-1001.0-py3-none-any.whl"]["core-metadata"] == {
        "sha256": upstream_metadata_hash
    }
