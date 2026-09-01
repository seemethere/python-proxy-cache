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
async def test_health_ping_does_not_degrade_on_transient_failure():
    c = Cache(backend="redis")
    fake = FakeRedis(fail_ping=True)
    c._redis = fake  # ty: ignore[invalid-assignment]
    c.state = CacheState.CONNECTED
    assert await c.health_ping() is False
    # Client must remain so a later successful ping can recover without reconnect().
    assert c._redis is fake
    assert c.state == CacheState.CONNECTED


@pytest.mark.asyncio
async def test_health_polls_do_not_inflate_request_error_counter():
    """A polled /health during an outage must not look like request-path failures."""
    c = Cache(backend="redis")
    c._redis = None
    c.state = CacheState.DEGRADED

    async def always_fails():
        c.redis_probe_errors += 1
        c.state = CacheState.DEGRADED
        return False

    c.reconnect = always_fails  # ty: ignore[invalid-assignment]
    for _ in range(5):
        assert await c.health_ping() is False
    assert c.redis_errors == 0
    assert c.redis_probe_errors == 5


@pytest.mark.asyncio
async def test_request_path_failure_counts_as_redis_error():
    c = Cache(backend="redis")
    c._redis = FakeRedis(fail_get=True)  # ty: ignore[invalid-assignment]
    c.state = CacheState.CONNECTED
    assert await c.get("k") is None
    assert c.redis_errors == 1
    assert c.redis_probe_errors == 0


@pytest.mark.asyncio
async def test_ping_false_when_degraded_reconnect_fails():
    c = Cache(backend="redis")
    c._redis = None
    c.state = CacheState.DEGRADED
    # reconnect will try real redis URL and fail in tests -> still degraded
    assert await c.health_ping() is False
    assert c.state == CacheState.DEGRADED


@pytest.mark.asyncio
async def test_connect_ping_success():
    c = Cache(backend="redis")
    fake = FakeRedis()
    c._redis = fake  # ty: ignore[invalid-assignment]
    await c.connect()
    assert c.state == CacheState.CONNECTED


@pytest.mark.asyncio
async def test_required_redis_retries_startup_until_connected(monkeypatch):
    c = Cache(backend="redis_required")
    c.startup_max_attempts = 3
    c.startup_retry_delay_seconds = 0.25
    outcomes = iter([False, False, True])
    sleeps: list[float] = []

    async def reconnect():
        return next(outcomes)

    async def sleep(delay: float):
        sleeps.append(delay)

    c.reconnect = reconnect  # ty: ignore[invalid-assignment]
    monkeypatch.setattr("app.cache.asyncio.sleep", sleep)

    await c.connect()

    assert sleeps == [0.25, 0.25]


@pytest.mark.asyncio
async def test_required_redis_startup_retries_are_bounded(monkeypatch):
    c = Cache(backend="redis_required")
    c.startup_max_attempts = 3
    c.startup_retry_delay_seconds = 0.25
    reconnects = 0
    sleeps: list[float] = []

    async def reconnect():
        nonlocal reconnects
        reconnects += 1
        return False

    async def sleep(delay: float):
        sleeps.append(delay)

    c.reconnect = reconnect  # ty: ignore[invalid-assignment]
    monkeypatch.setattr("app.cache.asyncio.sleep", sleep)

    with pytest.raises(
        RuntimeError, match="redis required but connect/ping failed after 3 attempts"
    ):
        await c.connect()

    assert reconnects == 3
    assert sleeps == [0.25, 0.25]


@pytest.mark.asyncio
async def test_optional_redis_does_not_retry_startup(monkeypatch):
    c = Cache(backend="redis")
    c.startup_max_attempts = 3
    c.startup_retry_delay_seconds = 0.25
    reconnects = 0

    async def reconnect():
        nonlocal reconnects
        reconnects += 1
        return False

    async def unexpected_sleep(_delay: float):
        pytest.fail("optional Redis startup should not sleep")

    c.reconnect = reconnect  # ty: ignore[invalid-assignment]
    monkeypatch.setattr("app.cache.asyncio.sleep", unexpected_sleep)

    await c.connect()

    assert reconnects == 1


@pytest.mark.asyncio
async def test_reconnect_after_degrade():
    c = Cache(backend="redis")
    c._redis = None
    c.state = CacheState.DEGRADED
    fake = FakeRedis()

    async def fake_reconnect():
        c._redis = fake  # ty: ignore[invalid-assignment]
        await fake.ping()
        c.state = CacheState.CONNECTED
        return True

    c.reconnect = fake_reconnect  # ty: ignore[invalid-assignment]
    assert await c.health_ping() is True
    assert c.state == CacheState.CONNECTED


@pytest.mark.asyncio
async def test_health_includes_cache_fields(client):
    r = await client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["cache_backend"] == "memory"
    assert data["cache_state"] == "memory"
    assert data["redis"] is False
    body = (await client.get("/metrics")).text
    assert "proxy_cache_redis_errors_total" in body
    assert "proxy_cache_redis_probe_errors_total" in body


@pytest.mark.asyncio
async def test_required_redis_health_reports_failed_current_probe(client, monkeypatch):
    from app.cache import cache

    async def failed_ping() -> bool:
        return False

    monkeypatch.setattr(cache, "backend", "redis_required")
    monkeypatch.setattr(cache, "state", CacheState.CONNECTED)
    monkeypatch.setattr(cache, "health_ping", failed_ping)

    response = await client.get("/health")

    assert response.status_code == 503
    assert response.json()["redis"] is False
    assert response.json()["cache_state"] == "degraded"
    # Reporting the failed probe does not discard a potentially recoverable client.
    assert cache.state == CacheState.CONNECTED
