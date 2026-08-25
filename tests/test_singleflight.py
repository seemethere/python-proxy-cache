from __future__ import annotations

import asyncio

from httpx import Response


async def test_concurrent_miss_single_upstream_fetch(client, mock_upstream):
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(url, headers=None):
        started.set()
        await release.wait()
        return Response(
            200,
            text=(
                "<!DOCTYPE html><html><body>"
                '<a href="https://files.pythonhosted.org/packages/a.whl#sha256=abc">a.whl</a>'
                "</body></html>"
            ),
            headers={"content-type": "text/html"},
        )

    rec = mock_upstream(handler)

    async def one():
        return await client.get("/simple/stampede/", headers={"Accept": "text/html"})

    tasks = [asyncio.create_task(one()) for _ in range(8)]
    await started.wait()
    # Give waiters time to queue on the miss lock while leader is blocked.
    await asyncio.sleep(0.05)
    release.set()
    results = await asyncio.gather(*tasks)
    assert all(r.status_code == 200 for r in results)
    assert rec.call_count == 1

    # Follow-up should be HIT without another upstream call.
    r = await client.get("/simple/stampede/", headers={"Accept": "text/html"})
    assert "HIT" in r.headers.get("x-cache", "")
    assert rec.call_count == 1


async def test_concurrent_miss_on_404_fetches_once(client, mock_upstream):
    """A 404 stampede must also collapse — the negative cache is filled under the lock."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(url, headers=None):
        started.set()
        await release.wait()
        return Response(404, text="Not Found")

    rec = mock_upstream(handler)

    tasks = [
        asyncio.create_task(client.get("/simple/ghost/", headers={"Accept": "text/html"}))
        for _ in range(6)
    ]
    await started.wait()
    await asyncio.sleep(0.05)
    release.set()
    results = await asyncio.gather(*tasks)

    assert all(r.status_code == 404 for r in results)
    assert rec.call_count == 1


async def test_locks_are_released_and_not_leaked(client, mock_upstream):
    """The lock map must not grow without bound across distinct project names."""
    from app.singleflight import miss_locks

    mock_upstream(
        lambda url, headers=None: Response(
            200,
            text='<!DOCTYPE html><html><body><a href="https://files.pythonhosted.org/a.whl#sha256=abc">a.whl</a></body></html>',
            headers={"content-type": "text/html"},
        )
    )
    for i in range(20):
        r = await client.get(f"/simple/proj{i}/", headers={"Accept": "text/html"})
        assert r.status_code == 200
    assert miss_locks._locks == {}
    assert miss_locks._waiters == {}
