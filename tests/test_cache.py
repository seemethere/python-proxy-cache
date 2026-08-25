from __future__ import annotations

import pytest

from app.cache import Cache, CacheState


class FakeRedis:
    def __init__(self, *, fail_get=False, fail_set=False, fail_ping=False):
        self.store: dict[str, str] = {}
        self.fail_get = fail_get
        self.fail_set = fail_set
        self.fail_ping = fail_ping

    async def ping(self):
        if self.fail_ping:
            raise ConnectionError("ping failed")
        return True

    async def get(self, key: str):
        if self.fail_get:
            raise ConnectionError("get failed")
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        if self.fail_set:
            raise ConnectionError("set failed")
        self.store[key] = value

    async def delete(self, key: str):
        self.store.pop(key, None)


@pytest.mark.asyncio
async def test_memory_backend_ignores_redis():
    c = Cache(backend="memory")
    assert c.state == CacheState.MEMORY
    assert c._redis is None
    await c.setex("k", 60, "v")
    assert await c.get("k") == "v"
    assert await c.ping() is False


@pytest.mark.asyncio
async def test_redis_get_failure_degrades_to_memory():
    c = Cache(backend="redis")
    fake = FakeRedis()
    c._redis = fake  # ty: ignore[invalid-assignment]
    c.state = CacheState.CONNECTED
    await c.setex("k", 60, "v")
    assert fake.store["k"] == "v"

    # Simulate outage on get after a successful set (value only in redis, not mem).
    fake.fail_get = True
    assert await c.get("k") is None
    assert c.state == CacheState.DEGRADED
    assert c._redis is None
    assert c.redis_errors >= 1

    # Further writes go to memory.
    await c.setex("k2", 60, "v2")
    assert await c.get("k2") == "v2"


@pytest.mark.asyncio
async def test_redis_set_failure_writes_memory():
    c = Cache(backend="redis")
    fake = FakeRedis(fail_set=True)
    c._redis = fake  # ty: ignore[invalid-assignment]
    c.state = CacheState.CONNECTED
    await c.setex("k", 60, "v")
    assert c.state == CacheState.DEGRADED
    assert await c.get("k") == "v"


@pytest.mark.asyncio
async def test_ping_false_when_redis_errors():
    c = Cache(backend="redis")
    fake = FakeRedis(fail_ping=True)
    c._redis = fake  # ty: ignore[invalid-assignment]
    c.state = CacheState.CONNECTED
    assert await c.ping() is False
    assert c.state == CacheState.DEGRADED


@pytest.mark.asyncio
async def test_connect_ping_success():
    c = Cache(backend="redis")
    fake = FakeRedis()
    c._redis = fake  # ty: ignore[invalid-assignment]
    await c.connect()
    assert c.state == CacheState.CONNECTED


@pytest.mark.asyncio
async def test_health_includes_cache_fields(client):
    r = await client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["cache_backend"] == "memory"
    assert data["cache_state"] == "memory"
    assert data["redis"] is False
    assert "proxy_cache_redis_errors_total" in (await client.get("/metrics")).text
