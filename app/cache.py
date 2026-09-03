from __future__ import annotations

import asyncio
import logging
import math
import time
from enum import StrEnum
from typing import Literal

try:
    import redis.asyncio as redis

    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False

from app.config import settings

logger = logging.getLogger(__name__)

CacheBackendName = Literal["memory", "redis", "redis_required"]


class CacheState(StrEnum):
    MEMORY = "memory"
    DISCONNECTED = "disconnected"  # redis configured, not yet connected
    CONNECTED = "connected"
    DEGRADED = "degraded"  # redis was configured but failed; may reconnect


class Cache:
    def __init__(
        self,
        redis_url: str | None = None,
        backend: CacheBackendName | None = None,
    ):
        self._redis = None
        self._mem: dict[str, tuple[float, str]] = {}
        self.url = redis_url or settings.redis_url
        self.backend: CacheBackendName = backend or settings.cache_backend
        self.startup_max_attempts = settings.redis_startup_max_attempts
        self.startup_retry_delay_seconds = settings.redis_startup_retry_delay_seconds
        self.state = CacheState.MEMORY
        # Request-path failures only. Health probes bump redis_probe_errors so a
        # polled /health during an outage cannot inflate the request-path metric.
        self.redis_errors = 0
        self.redis_probe_errors = 0

        if self.backend == "memory":
            self._redis = None
            self.state = CacheState.MEMORY
            return

        if not HAS_REDIS:
            if self.backend == "redis_required":
                raise RuntimeError(
                    "cache_backend=redis_required but redis package is not installed"
                )
            logger.warning("redis package missing; using in-memory cache")
            self.state = CacheState.MEMORY
            return

        try:
            self._redis = redis.from_url(self.url, decode_responses=True, socket_connect_timeout=1)
            self.state = CacheState.DISCONNECTED
        except Exception:
            self._redis = None
            if self.backend == "redis_required":
                raise
            logger.exception("failed to construct redis client; using in-memory cache")
            self.state = CacheState.MEMORY

    async def connect(self) -> None:
        """Eager ping. Call from app lifespan."""
        if self.backend == "memory":
            self.state = CacheState.MEMORY
            return

        attempts = self.startup_max_attempts if self.backend == "redis_required" else 1
        for attempt in range(1, attempts + 1):
            if await self.reconnect():
                return
            if attempt < attempts:
                logger.info(
                    "redis required at startup; retrying in %s seconds (%d/%d)",
                    self.startup_retry_delay_seconds,
                    attempt + 1,
                    attempts,
                )
                await asyncio.sleep(self.startup_retry_delay_seconds)

        if self.backend == "redis_required":
            raise RuntimeError(f"redis required but connect/ping failed after {attempts} attempts")

    async def reconnect(self) -> bool:
        """(Re)build Redis client and ping. Used after degrade or at startup.

        Counts against redis_probe_errors, not redis_errors: /health polls this on
        every tick while Redis is down, so charging the request-path counter would
        make it measure poll frequency instead of actual request failures.
        """
        if self.backend == "memory" or not HAS_REDIS:
            self.state = CacheState.MEMORY
            return False
        try:
            if self._redis is None:
                self._redis = redis.from_url(
                    self.url, decode_responses=True, socket_connect_timeout=1
                )
            await self._redis.ping()
            self.state = CacheState.CONNECTED
            return True
        except Exception as e:
            self.redis_probe_errors += 1
            # No traceback: this fires once per health poll for the whole outage.
            logger.warning("redis reconnect/ping failed (%s); staying degraded", e)
            self._redis = None
            self.state = CacheState.DEGRADED
            return False

    def _degrade(self, exc: Exception) -> None:
        self.redis_errors += 1
        logger.warning("redis error (%s); degrading to in-memory until reconnect", exc)
        self._redis = None
        self.state = CacheState.DEGRADED

    async def health_ping(self) -> bool:
        """Non-mutating probe for /health. Does not clear the Redis client on failure.

        If currently degraded, attempts a reconnect (recover from transient outages).
        """
        if self.backend == "memory":
            return False
        if self.state == CacheState.DEGRADED or self._redis is None:
            return await self.reconnect()
        try:
            await self._redis.ping()
            self.state = CacheState.CONNECTED
            return True
        except Exception:
            # Leave client in place; request path may still degrade on use.
            return False

    async def ping(self) -> bool:
        """Legacy alias used by older callers; prefer health_ping for /health."""
        return await self.health_ping()

    async def get(self, key: str) -> str | None:
        if self._redis is not None:
            try:
                v = await self._redis.get(key)
                if v is not None:
                    if isinstance(v, bytes):
                        return v.decode()
                    return str(v)
                return None
            except Exception as e:
                self._degrade(e)
        # Reached only when Redis is absent or just degraded. A live Redis miss returns
        # None above and deliberately does not consult _mem: entries written to _mem
        # during an earlier degraded window are shadowed once Redis is back, so a
        # recovered node re-fetches rather than serving from a divergent local copy.
        if key in self._mem:
            exp, val = self._mem[key]
            if exp > time.time():
                return val
            del self._mem[key]
        return None

    async def setex(self, key: str, ttl: int, value: str) -> None:
        if self._redis is not None:
            try:
                await self._redis.set(key, value, ex=ttl)
                return
            except Exception as e:
                self._degrade(e)
        self._mem[key] = (time.time() + ttl, value)

    async def ttl(self, key: str) -> int | None:
        """Return the remaining whole-second lifetime for a live key."""
        if self._redis is not None:
            try:
                remaining = int(await self._redis.ttl(key))
                return remaining if remaining > 0 else None
            except Exception as e:
                self._degrade(e)
        entry = self._mem.get(key)
        if entry is None:
            return None
        remaining = entry[0] - time.time()
        if remaining <= 0:
            del self._mem[key]
            return None
        remaining_seconds = math.floor(remaining)
        return remaining_seconds if remaining_seconds > 0 else None

    async def ttl_if_values(
        self,
        key: str,
        expected: str,
        bound_key: str,
        expected_bound: str,
    ) -> int | None:
        """Return the shorter TTL only when both keys still have their expected values."""
        if self._redis is not None:
            script = """
                if redis.call('GET', KEYS[1]) ~= ARGV[1] or
                   redis.call('GET', KEYS[2]) ~= ARGV[2] then
                    return -1
                end
                local marker_ttl = redis.call('TTL', KEYS[1])
                local bound_ttl = redis.call('TTL', KEYS[2])
                if marker_ttl <= 0 or bound_ttl <= 0 then
                    return -1
                end
                return math.min(marker_ttl, bound_ttl)
            """
            try:
                remaining = int(
                    await self._redis.eval(script, 2, key, bound_key, expected, expected_bound)
                )
                return remaining if remaining > 0 else None
            except Exception as e:
                self._degrade(e)

        now = time.time()
        marker = self._mem.get(key)
        bound = self._mem.get(bound_key)
        if (
            marker is None
            or marker[0] <= now
            or marker[1] != expected
            or bound is None
            or bound[0] <= now
            or bound[1] != expected_bound
        ):
            return None
        remaining = math.floor(min(marker[0], bound[0]) - now)
        return remaining if remaining > 0 else None

    async def setex_many_if_unchanged(
        self,
        expected: dict[str, str | None],
        values: list[tuple[str, int, str]],
    ) -> bool:
        """Atomically write expiring values if all observed keys are unchanged."""
        if self._redis is not None:
            script = """
                local n = tonumber(ARGV[1])
                for i = 1, n do
                    local current = redis.call('GET', KEYS[i])
                    local present = ARGV[2 + (i - 1) * 2]
                    local expected_value = ARGV[3 + (i - 1) * 2]
                    if (present == '0' and current) or
                       (present == '1' and current ~= expected_value) then
                        return 0
                    end
                end
                local offset = 2 + n * 2
                for i = 1, #KEYS - n do
                    local ttl = ARGV[offset + (i - 1) * 2]
                    local value = ARGV[offset + 1 + (i - 1) * 2]
                    redis.call('SET', KEYS[n + i], value, 'EX', ttl)
                end
                return 1
            """
            expected_items = list(expected.items())
            keys = [key for key, _ in expected_items] + [key for key, _, _ in values]
            args: list[str | int] = [len(expected_items)]
            for _, value in expected_items:
                args.extend((0, "")) if value is None else args.extend((1, value))
            for _, ttl, value in values:
                args.extend((ttl, value))
            try:
                return bool(await self._redis.eval(script, len(keys), *keys, *args))
            except Exception as e:
                self._degrade(e)

        now = time.time()
        for key, expected_value in expected.items():
            entry = self._mem.get(key)
            current = entry[1] if entry is not None and entry[0] > now else None
            if current != expected_value:
                return False
        for key, ttl, value in values:
            self._mem[key] = (now + ttl, value)
        return True


cache = Cache()
