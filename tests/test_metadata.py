from __future__ import annotations

from httpx import Response

from app.cache import cache
from app.config import settings
from app.metadata import (
    _artifact_upstream_url,
    _extraction_candidate_indexes,
    _extraction_pending,
    _pending,
    _probe,
    _safe_url_for_log,
    cancel_metadata_tasks,
    drain_metadata_tasks,
    load_metadata_for_url,
    metadata_head_url,
    metadata_response_cache_ttl,
    schedule_metadata_enrichment,
    start_metadata_tasks,
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


def test_recovery_and_background_failures_have_separate_keys():
    import app.metadata as meta

    artifact_url = "/artifacts/files.pythonhosted.org/packages/a.whl"

    assert meta._metadata_failure_key(artifact_url, None) != meta._metadata_failure_key(
        artifact_url, "a" * 64
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
    assert "accept" in r.headers["vary"].lower()
    await drain_metadata_tasks()
    assert all(c["method"] == "GET" for c in rec.calls)


async def test_metadata_probe_on_enriches_cache(client, mock_upstream, monkeypatch):
    import asyncio

    monkeypatch.setattr(settings, "enable_background_metadata", True)
    html = (
        "<!DOCTYPE html><html><body>"
        f'<a href="https://files.pythonhosted.org/packages/a-1.0-py3-none-any.whl'
        f'#sha256={"a" * 64}">a-1.0-py3-none-any.whl</a>'
        "</body></html>"
    )

    def get_handler(url, headers=None):
        return Response(200, text=html, headers={"content-type": "text/html"})

    head_started = asyncio.Event()
    release_head = asyncio.Event()

    async def head_handler(url, headers=None):
        assert str(url).endswith(".metadata")
        head_started.set()
        await release_head.wait()
        return Response(200)

    rec = mock_upstream(get_handler, head_handler=head_handler)
    r = await client.get(
        "/simple/metaon/", headers={"Accept": "application/vnd.pypi.simple.v1+json"}
    )
    assert r.status_code == 200
    # Hot path still advertises false until enrichment finishes
    assert r.json()["files"][0].get("core-metadata") is False
    assert r.headers["cache-control"] == "public, max-age=1"
    assert "accept" in r.headers["vary"].lower()

    await head_started.wait()
    pending = await client.get(
        "/simple/metaon/", headers={"Accept": "application/vnd.pypi.simple.v1+json"}
    )
    assert pending.headers["x-cache"] == "HIT"
    assert pending.json()["files"][0]["core-metadata"] is False
    assert pending.headers["cache-control"] == "public, max-age=1"
    assert "accept" in pending.headers["vary"].lower()

    release_head.set()
    await drain_metadata_tasks()
    assert any(c["method"] == "HEAD" for c in rec.calls)

    # Read the exact representation the background task enriched.
    body = await cache.get("simple:metaon:json")
    assert body is not None
    import json

    data = json.loads(body)
    assert data["files"][0]["core-metadata"] is True

    cached_json = await client.get(
        "/simple/metaon/", headers={"Accept": "application/vnd.pypi.simple.v1+json"}
    )
    assert cached_json.json()["files"][0]["core-metadata"] is True
    assert cached_json.headers["cache-control"].startswith("public, max-age=")
    assert "accept" in cached_json.headers["vary"].lower()

    pending_html = await client.get("/simple/metaon/", headers={"Accept": "text/html"})
    assert pending_html.headers["cache-control"] == "public, max-age=1"
    await drain_metadata_tasks()
    cached_html = await client.get("/simple/metaon/", headers={"Accept": "text/html"})
    assert 'data-core-metadata="true"' in cached_html.text
    assert cached_html.headers["cache-control"].startswith("public, max-age=")
    assert "accept" in cached_html.headers["vary"].lower()


async def test_completed_noop_enrichment_makes_exact_body_cacheable(
    client, mock_upstream, monkeypatch
):
    monkeypatch.setattr(settings, "enable_background_metadata", True)
    html = (
        "<!DOCTYPE html><html><body>"
        '<a href="https://files.pythonhosted.org/packages/a-1.0-py3-none-any.whl" '
        'data-core-metadata="true">a-1.0-py3-none-any.whl</a>'
        "</body></html>"
    )
    mock_upstream(
        lambda url, headers=None: Response(200, text=html, headers={"content-type": "text/html"})
    )

    first = await client.get("/simple/native/", headers={"Accept": "text/html"})
    assert first.headers["cache-control"] == "public, max-age=1"
    await drain_metadata_tasks()

    second = await client.get("/simple/native/", headers={"Accept": "text/html"})
    assert second.headers["cache-control"].startswith("public, max-age=")


async def test_dropped_enrichment_uses_only_pending_microcache(client, mock_upstream, monkeypatch):
    monkeypatch.setattr(settings, "enable_background_metadata", True)
    monkeypatch.setattr(settings, "metadata_pending_cache_ttl_seconds", 3)
    monkeypatch.setattr(settings, "metadata_max_pending_projects", 0)
    html = (
        "<!DOCTYPE html><html><body>"
        f'<a href="https://files.pythonhosted.org/packages/a-1.0-py3-none-any.whl'
        f'#sha256={"a" * 64}">a-1.0-py3-none-any.whl</a>'
        "</body></html>"
    )
    mock_upstream(
        lambda url, headers=None: Response(200, text=html, headers={"content-type": "text/html"})
    )

    first = await client.get("/simple/dropped/", headers={"Accept": "text/html"})
    second = await client.get("/simple/dropped/", headers={"Accept": "text/html"})

    assert first.headers["cache-control"] == "public, max-age=3"
    assert second.headers["x-cache"] == "HIT"
    assert second.headers["cache-control"] == "public, max-age=3"


async def test_pending_microcache_can_be_disabled(client, mock_upstream, monkeypatch):
    monkeypatch.setattr(settings, "enable_background_metadata", True)
    monkeypatch.setattr(settings, "metadata_pending_cache_ttl_seconds", 0)
    monkeypatch.setattr(settings, "metadata_max_pending_projects", 0)
    mock_upstream(
        lambda url, headers=None: Response(
            200,
            text="<!DOCTYPE html><html><body></body></html>",
            headers={"content-type": "text/html"},
        )
    )

    response = await client.get("/simple/no-pending-cache/", headers={"Accept": "text/html"})

    assert response.headers["cache-control"] == "no-store"


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
    orig_batch = cache.setex_many
    orig_many = cache.setex_many_if_unchanged

    async def spy(key: str, ttl: int, value: str):
        recorded.append((key, ttl))
        return await orig(key, ttl, value)

    async def spy_batch(values):
        recorded.extend((key, ttl) for key, ttl, _ in values)
        return await orig_batch(values)

    async def spy_many(expected, values):
        recorded.extend((key, ttl) for key, ttl, _ in values)
        return await orig_many(expected, values)

    monkeypatch.setattr(cache, "setex", spy)
    monkeypatch.setattr(cache, "setex_many", spy_batch)
    monkeypatch.setattr(cache, "setex_many_if_unchanged", spy_many)
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
    # Enrichment may preserve only the source's remaining lifetime; it must
    # never restart the full upstream-derived TTL.
    assert primary[0] == 7
    assert all(1 <= value <= 7 for value in primary)
    assert primary[-1] <= primary[0]
    cached = await client.get("/simple/ttlcheck/", headers={"Accept": "text/html"})
    max_age = int(cached.headers["cache-control"].rsplit("=", 1)[1])
    assert 1 < max_age <= 7


async def test_enrichment_does_not_clobber_a_newer_body(client, mock_upstream, monkeypatch):
    """A slow enrichment must not overwrite an entry a later fill already replaced."""
    monkeypatch.setattr(settings, "enable_background_metadata", True)
    from app.metadata import _enrich

    stale_source = "<html><body>OLD</body></html>"
    await cache.setex("simple:racy:html", 60, "<html><body>NEW</body></html>")
    # enrich from a body that is no longer what is cached
    await _enrich("racy", stale_source, is_json=False, ttl=60)
    assert await cache.get("simple:racy:html") == "<html><body>NEW</body></html>"
    assert (
        await metadata_response_cache_ttl("racy", "<html><body>NEW</body></html>", is_json=False)
        is None
    )


async def test_invalid_or_wrong_readiness_marker_is_not_cacheable():
    body = "<html><body>exact</body></html>"
    await cache.setex("simple:marker:html", 60, body)
    await cache.setex("simple:marker:html:metadata-ready", 60, "not-a-body-digest")
    assert await metadata_response_cache_ttl("marker", body, is_json=False) is None


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
        async def head(self, url, **kwargs):
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

    monkeypatch.setattr(meta, "_cache_semaphore", asyncio.Semaphore(3))
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
        async def head(self, url, **kwargs):
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


async def test_v1_metadata_records_remain_readable():
    import hashlib
    import json

    import app.metadata as meta

    artifact_url = "/artifacts/files.pythonhosted.org/packages/legacy.whl"
    wheel_sha256 = "a" * 64
    body = "Metadata-Version: 2.4\nName: legacy\nVersion: 1.0\n"
    metadata_sha256 = hashlib.sha256(body.encode()).hexdigest()
    await cache.setex(meta._metadata_blob_key(artifact_url, wheel_sha256), 60, body)
    await cache.setex(
        meta._metadata_url_key(artifact_url),
        60,
        json.dumps({"wheel-sha256": wheel_sha256, "metadata-sha256": metadata_sha256}),
    )

    assert await load_metadata_for_url(artifact_url) == (body, metadata_sha256)


async def test_v2_metadata_record_rejects_corrupt_content():
    import json

    import app.metadata as meta

    artifact_url = "/artifacts/files.pythonhosted.org/packages/corrupt.whl"
    metadata_sha256 = "b" * 64
    await cache.setex(
        meta._metadata_content_key(metadata_sha256),
        60,
        "not the content matching that digest",
    )
    await cache.setex(
        meta._metadata_url_key(artifact_url),
        60,
        json.dumps({"schema": 2, "metadata-sha256": metadata_sha256}),
    )

    assert await load_metadata_for_url(artifact_url) is None


async def test_v2_record_falls_back_to_rolling_compatible_body():
    import app.metadata as meta

    artifact_url = "/artifacts/files.pythonhosted.org/packages/rolling.whl"
    content = b"Metadata-Version: 2.4\nName: rolling\nVersion: 1.0\n"
    wheel_sha256 = "c" * 64
    await store_extracted_metadata(artifact_url, wheel_sha256, content)
    stored = await load_metadata_for_url(artifact_url)
    assert stored is not None
    metadata_sha256 = stored[1]
    cache._mem.pop(meta._metadata_content_key(metadata_sha256))

    assert await load_metadata_for_url(artifact_url) == (content.decode(), metadata_sha256)


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


async def test_metadata_route_recovers_after_cache_local_miss(client, mock_upstream, monkeypatch):
    import hashlib

    import app.metadata as meta

    monkeypatch.setattr(settings, "enable_background_metadata", True)
    artifact_url = "/artifacts/files.pythonhosted.org/packages/demo.whl?signature=old"
    content = b"Metadata-Version: 2.4\nName: demo\nVersion: 1.0\n"
    expected = hashlib.sha256(content).hexdigest()
    assert await store_extracted_metadata(artifact_url, "a" * 64, content) == {"sha256": expected}

    # Simulate a request landing on another process with an isolated cache.
    cache._mem.clear()
    recorder = mock_upstream(
        lambda url, headers=None: Response(404),
        head_handler=lambda url, headers=None: Response(404),
    )
    extracted: list[str] = []

    async def fake_extract(url: str) -> bytes:
        extracted.append(url)
        return content

    monkeypatch.setattr(meta, "_extract_wheel_metadata", fake_extract)
    url = "/artifacts/files.pythonhosted.org/packages/demo.whl.metadata?signature=renewed"
    response = await client.get(url)

    assert response.status_code == 200
    assert response.content == content
    assert response.headers["etag"] == f'"sha256:{expected}"'
    assert extracted == ["https://files.pythonhosted.org/packages/demo.whl?signature=renewed"]
    head = [call for call in recorder.calls if call["method"] == "HEAD"]
    assert head[0]["url"] == (
        "https://files.pythonhosted.org/packages/demo.whl.metadata?signature=renewed"
    )
    assert head[0]["kwargs"] == {"follow_redirects": False}

    # Recovery publishes the content before its URL record, so the next request
    # is a cache hit and performs no additional upstream work.
    second = await client.get(url)
    assert second.status_code == 200
    assert len(recorder.calls) == 1
    assert extracted == ["https://files.pythonhosted.org/packages/demo.whl?signature=renewed"]


async def test_metadata_route_preserves_native_sidecar_fallback(client, mock_upstream, monkeypatch):
    import app.metadata as meta

    monkeypatch.setattr(settings, "enable_background_metadata", True)
    recorder = mock_upstream(
        lambda url, headers=None: Response(200),
        head_handler=lambda url, headers=None: Response(200),
    )

    async def unexpected_extract(url: str) -> bytes:
        raise AssertionError(f"native metadata must not extract {url}")

    monkeypatch.setattr(meta, "_extract_wheel_metadata", unexpected_extract)
    response = await client.get("/artifacts/files.pythonhosted.org/packages/native.whl.metadata")

    # nginx interprets this internal 404 as its signal to fetch the canonical
    # native sidecar from the artifact upstream.
    assert response.status_code == 404
    assert recorder.calls[0]["method"] == "HEAD"
    assert recorder.calls[0]["kwargs"] == {"follow_redirects": False}


async def test_metadata_route_recovery_is_singleflight(client, mock_upstream, monkeypatch):
    import asyncio

    import app.metadata as meta

    monkeypatch.setattr(settings, "enable_background_metadata", True)
    recorder = mock_upstream(
        lambda url, headers=None: Response(400),
        head_handler=lambda url, headers=None: Response(400),
    )
    content = b"Metadata-Version: 2.4\nName: demo\nVersion: 1.0\n"
    extraction_started = asyncio.Event()
    release = asyncio.Event()
    extractions = 0
    attempts_before = metrics["metadata_recovery_attempts"]
    successes_before = metrics["metadata_recovery_successes"]

    async def blocked_extract(url: str) -> bytes:
        nonlocal extractions
        extractions += 1
        extraction_started.set()
        await release.wait()
        return content

    monkeypatch.setattr(meta, "_extract_wheel_metadata", blocked_extract)
    url = "/artifacts/files.pythonhosted.org/packages/concurrent.whl.metadata"
    requests = [asyncio.create_task(client.get(url)) for _ in range(8)]
    await extraction_started.wait()
    release.set()
    responses = await asyncio.gather(*requests)

    assert [response.status_code for response in responses] == [200] * 8
    assert {response.content for response in responses} == {content}
    assert extractions == 1
    assert len([call for call in recorder.calls if call["method"] == "HEAD"]) == 1
    assert metrics["metadata_recovery_attempts"] == attempts_before + 1
    assert metrics["metadata_recovery_successes"] == successes_before + 1


async def test_metadata_cache_hit_bypasses_saturated_probe_pool(monkeypatch):
    import asyncio

    import app.metadata as meta

    monkeypatch.setattr(settings, "enable_background_metadata", True)
    artifact_url = "/artifacts/files.pythonhosted.org/packages/hot.whl"
    content = b"Metadata-Version: 2.4\nName: hot\nVersion: 1.0\n"
    await store_extracted_metadata(artifact_url, "d" * 64, content)
    monkeypatch.setattr(meta, "_probe_semaphore", asyncio.Semaphore(0))

    stored = await asyncio.wait_for(meta.load_or_recover_metadata_for_url(artifact_url), 0.1)

    assert stored is not None
    assert stored[0].encode() == content


async def test_metadata_recovery_bypasses_saturated_background_extraction_pool(
    client, mock_upstream, monkeypatch
):
    import asyncio

    import app.metadata as meta

    monkeypatch.setattr(settings, "enable_background_metadata", True)
    monkeypatch.setattr(meta, "_extract_semaphore", asyncio.Semaphore(0))
    mock_upstream(
        lambda url, headers=None: Response(404),
        head_handler=lambda url, headers=None: Response(404),
    )
    content = b"Metadata-Version: 2.4\nName: reserved\nVersion: 1.0\n"

    async def extract(url: str) -> bytes:
        return content

    monkeypatch.setattr(meta, "_extract_wheel_metadata", extract)
    response = await asyncio.wait_for(
        client.get("/artifacts/files.pythonhosted.org/packages/reserved.whl.metadata"),
        0.1,
    )

    assert response.status_code == 200
    assert response.content == content


async def test_background_probe_rejects_metadata_for_different_wheel_hash(monkeypatch):
    import hashlib

    import app.metadata as meta

    artifact_url = "/artifacts/files.pythonhosted.org/packages/replaced.whl"
    old_content = b"Metadata-Version: 2.4\nName: replaced\nVersion: 1.0\n"
    new_content = b"Metadata-Version: 2.4\nName: replaced\nVersion: 2.0\n"
    await store_extracted_metadata(artifact_url, "a" * 64, old_content)

    class FakeClient:
        async def head(self, url, **kwargs):
            return Response(404)

    extracted: list[str] = []

    async def fake_extract(url: str) -> bytes:
        extracted.append(url)
        return new_content

    monkeypatch.setattr(meta, "get_http_client", lambda: FakeClient())
    monkeypatch.setattr(meta, "_extract_wheel_metadata", fake_extract)
    file = File(
        filename="replaced.whl",
        url=artifact_url,
        hashes={"sha256": "b" * 64},
    )

    result = await _probe(file)

    assert extracted == ["https://files.pythonhosted.org/packages/replaced.whl"]
    assert result.core_metadata == {"sha256": hashlib.sha256(new_content).hexdigest()}


async def test_recovery_kill_switch_performs_no_upstream_io(client, mock_upstream, monkeypatch):
    import app.metadata as meta

    monkeypatch.setattr(settings, "enable_background_metadata", False)
    recorder = mock_upstream(lambda url, headers=None: Response(500))

    async def unexpected_extract(url: str) -> bytes:
        raise AssertionError(f"recovery disabled but extracted {url}")

    monkeypatch.setattr(meta, "_extract_wheel_metadata", unexpected_extract)
    response = await client.get("/artifacts/files.pythonhosted.org/packages/disabled.whl.metadata")

    assert response.status_code == 404
    assert recorder.calls == []


async def test_metadata_route_failure_is_negatively_cached(client, mock_upstream, monkeypatch):
    import app.metadata as meta

    monkeypatch.setattr(settings, "enable_background_metadata", True)
    recorder = mock_upstream(
        lambda url, headers=None: Response(404),
        head_handler=lambda url, headers=None: Response(404),
    )
    extractions = 0

    async def failed_extract(url: str) -> bytes:
        nonlocal extractions
        extractions += 1
        raise ValueError("malformed wheel")

    monkeypatch.setattr(meta, "_extract_wheel_metadata", failed_extract)
    url = "/artifacts/files.pythonhosted.org/packages/broken.whl.metadata"

    assert (await client.get(url)).status_code == 404
    assert (await client.get(url)).status_code == 404
    assert extractions == 1
    # Native metadata is still rechecked before consulting the extraction
    # failure cache, so a newly published native sidecar remains discoverable.
    assert len([call for call in recorder.calls if call["method"] == "HEAD"]) == 2


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
        async def head(self, url, **kwargs):
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


async def test_native_discovery_finishes_before_delayed_extraction(
    client, mock_upstream, monkeypatch
):
    import asyncio

    import app.metadata as meta

    await drain_metadata_tasks()
    monkeypatch.setattr(settings, "enable_background_metadata", True)
    extraction_release = asyncio.Event()
    extraction_waiting = asyncio.Event()
    extracted: list[str] = []
    html = (
        "<!DOCTYPE html><html><body>"
        f'<a href="https://files.pythonhosted.org/packages/demo-1.0-py3-none-any.whl'
        f'#sha256={"a" * 64}">demo-1.0-py3-none-any.whl</a>'
        "</body></html>"
    )

    async def wait_for_idle() -> None:
        extraction_waiting.set()
        await extraction_release.wait()

    async def extract(url: str) -> bytes:
        extracted.append(url)
        return b"Metadata-Version: 2.4\nName: demo\nVersion: 1.0\n"

    recorder = mock_upstream(
        lambda url, headers=None: Response(200, text=html, headers={"content-type": "text/html"}),
        head_handler=lambda url, headers=None: Response(404),
    )
    monkeypatch.setattr(meta, "_wait_for_extraction_idle", wait_for_idle)
    monkeypatch.setattr(meta, "_extract_wheel_metadata", extract)

    response = await client.get("/simple/phased/", headers={"Accept": "text/html"})
    assert response.status_code == 200
    await asyncio.wait_for(extraction_waiting.wait(), 1)

    assert extracted == []
    assert len(_extraction_pending) == 1
    assert len([call for call in recorder.calls if call["method"] == "HEAD"]) == 1
    assert await meta.metadata_response_cache_ttl("phased", html, is_json=False) is None

    # The exact discovery marker avoids repeating HEAD while extraction waits.
    await client.get("/simple/phased/", headers={"Accept": "text/html"})
    await asyncio.sleep(0)
    assert len([call for call in recorder.calls if call["method"] == "HEAD"]) == 1

    extraction_release.set()
    await drain_metadata_tasks()

    assert len(extracted) == 1
    assert len([call for call in recorder.calls if call["method"] == "HEAD"]) == 1
    enriched = await client.get("/simple/phased/", headers={"Accept": "text/html"})
    assert 'data-core-metadata="sha256=' in enriched.text
    assert enriched.headers["cache-control"].startswith("public, max-age=")


async def test_native_discovery_needs_no_extraction_job(client, mock_upstream, monkeypatch):
    import app.metadata as meta

    await drain_metadata_tasks()
    monkeypatch.setattr(settings, "enable_background_metadata", True)
    html = (
        "<!DOCTYPE html><html><body>"
        f'<a href="https://files.pythonhosted.org/packages/native-1.0-py3-none-any.whl'
        f'#sha256={"b" * 64}">native-1.0-py3-none-any.whl</a>'
        "</body></html>"
    )

    async def unexpected_extract(url: str) -> bytes:
        raise AssertionError(f"native discovery must not extract {url}")

    mock_upstream(
        lambda url, headers=None: Response(200, text=html, headers={"content-type": "text/html"}),
        head_handler=lambda url, headers=None: Response(200),
    )
    monkeypatch.setattr(meta, "_extract_wheel_metadata", unexpected_extract)

    await client.get("/simple/native-phase/", headers={"Accept": "text/html"})
    await drain_metadata_tasks()

    assert _extraction_pending == {}
    completed = await client.get("/simple/native-phase/", headers={"Accept": "text/html"})
    assert 'data-core-metadata="true"' in completed.text
    assert completed.headers["cache-control"].startswith("public, max-age=")


async def test_delayed_extraction_queue_coalesces_and_is_independently_bounded(monkeypatch):
    import asyncio

    import app.metadata as meta

    await drain_metadata_tasks()
    monkeypatch.setattr(settings, "metadata_max_pending_extraction_projects", 1)
    release = asyncio.Event()

    async def blocked_idle() -> None:
        await release.wait()

    monkeypatch.setattr(meta, "_wait_for_extraction_idle", blocked_idle)
    queued_before = metrics["metadata_extraction_jobs_queued"]
    coalesced_before = metrics["metadata_extraction_jobs_coalesced"]
    dropped_before = metrics["metadata_extraction_jobs_dropped"]

    meta._schedule_project_extraction("one", is_json=False, body="same")
    meta._schedule_project_extraction("one", is_json=False, body="same")
    meta._schedule_project_extraction("two", is_json=False, body="different")
    await asyncio.sleep(0)

    assert len(_extraction_pending) == 1
    assert metrics["metadata_extraction_jobs_queued"] == queued_before + 1
    assert metrics["metadata_extraction_jobs_coalesced"] == coalesced_before + 1
    assert metrics["metadata_extraction_jobs_dropped"] == dropped_before + 1

    release.set()
    await drain_metadata_tasks()


async def test_background_tasks_cancel_cleanly(monkeypatch):
    import asyncio

    import app.metadata as meta

    await drain_metadata_tasks()
    release = asyncio.Event()

    async def blocked_idle() -> None:
        await release.wait()

    monkeypatch.setattr(meta, "_wait_for_extraction_idle", blocked_idle)
    meta._schedule_project_extraction("cancelled", is_json=False, body="body")
    await asyncio.sleep(0)

    await cancel_metadata_tasks()

    assert _pending == {}
    assert _extraction_pending == {}
    start_metadata_tasks()


async def test_shutdown_rejects_new_background_work(monkeypatch):
    import app.metadata as meta

    await cancel_metadata_tasks()
    monkeypatch.setattr(settings, "enable_background_metadata", True)

    schedule_metadata_enrichment("discovery", "body", is_json=False, ttl=60)
    meta._schedule_project_extraction("extraction", is_json=False, body="body")

    try:
        assert _pending == {}
        assert _extraction_pending == {}
    finally:
        start_metadata_tasks()


async def test_extraction_idle_wait_uses_configured_project_quiet_period(monkeypatch):
    import app.metadata as meta

    class FakeLoop:
        def __init__(self):
            self.times = iter((10.0, 12.0))

        def time(self) -> float:
            return next(self.times)

    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(settings, "metadata_background_extraction_idle_seconds", 2.0)
    monkeypatch.setattr(meta, "_last_project_activity", 10.0)
    monkeypatch.setattr(meta.asyncio, "get_running_loop", lambda: FakeLoop())
    monkeypatch.setattr(meta.asyncio, "sleep", fake_sleep)

    await meta._wait_for_extraction_idle()

    assert sleeps == [2.0]


async def test_delayed_extraction_rechecks_idle_between_batches(monkeypatch):
    import app.metadata as meta

    monkeypatch.setattr(settings, "metadata_extract_concurrency", 1)
    body = (
        "<!DOCTYPE html><html><body>"
        + "".join(
            f'<a href="https://files.pythonhosted.org/packages/demo-{version}.0-py3-none-any.whl'
            f'#sha256={version:064x}">demo-{version}.0-py3-none-any.whl</a>'
            for version in (1, 2)
        )
        + "</body></html>"
    )
    await cache.setex("simple:batches:html", 60, body)
    idle_checks = 0

    async def record_idle_check() -> None:
        nonlocal idle_checks
        idle_checks += 1

    async def resolve(file, **kwargs):
        assert kwargs == {"allow_extraction": True, "check_native": False}
        return meta._with_core_metadata(file, True)

    monkeypatch.setattr(meta, "_wait_for_extraction_idle", record_idle_check)
    monkeypatch.setattr(meta, "_probe", resolve)

    await meta._extract_project_after_idle(
        "batches", is_json=False, body_digest=meta._body_digest(body)
    )

    # Once before and once after worker admission, then before each wheel batch.
    assert idle_checks == 4


async def test_ready_hits_and_404s_record_project_activity(client, mock_upstream, monkeypatch):
    import app.metadata as meta

    monkeypatch.setattr(settings, "enable_background_metadata", True)
    body = "<!DOCTYPE html><html><body></body></html>"
    await cache.setex("simple:activity:html", 60, body)
    await cache.setex(
        meta._project_ready_key("activity", is_json=False),
        60,
        meta._body_digest(body),
    )
    monkeypatch.setattr(meta, "_last_project_activity", 0.0)

    ready = await client.get("/simple/activity/", headers={"Accept": "text/html"})

    assert ready.status_code == 200
    assert meta._last_project_activity > 0

    cache._mem.clear()
    mock_upstream(lambda url, headers=None: Response(404))
    monkeypatch.setattr(meta, "_last_project_activity", 0.0)

    missing = await client.get("/simple/missing-activity/", headers={"Accept": "text/html"})

    assert missing.status_code == 404
    assert meta._last_project_activity > 0


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
