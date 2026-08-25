from __future__ import annotations

import logging
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
    CONNECTED = "connected"
    DEGRADED = "degraded"


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
        self.state = CacheState.MEMORY
        self.redis_errors = 0

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
            # Not connected yet — ping() in connect() / lifespan.
            self.state = CacheState.DEGRADED
        except Exception:
            self._redis = None
            if self.backend == "redis_required":
                raise
            logger.exception("failed to construct redis client; using in-memory cache")
            self.state = CacheState.MEMORY

    async def connect(self) -> None:
        """Eager ping. Call from app lifespan."""
        if self.backend == "memory" or self._redis is None:
            self.state = CacheState.MEMORY
            return
        try:
            await self._redis.ping()
            self.state = CacheState.CONNECTED
        except Exception:
            self.redis_errors += 1
            logger.exception("redis ping failed")
            if self.backend == "redis_required":
                self.state = CacheState.DEGRADED
                raise
            # Soft degrade: stop using redis for this process.
            self._redis = None
            self.state = CacheState.DEGRADED

    def _degrade(self, exc: Exception) -> None:
        self.redis_errors += 1
        logger.warning("redis error (%s); degrading to in-memory for this process", exc)
        self._redis = None
        self.state = CacheState.DEGRADED

    async def ping(self) -> bool:
        if self._redis is None:
            return False
        try:
            await self._redis.ping()
            self.state = CacheState.CONNECTED
            return True
        except Exception as e:
            self._degrade(e)
            return False

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

    async def delete(self, key: str) -> None:
        if self._redis is not None:
            try:
                await self._redis.delete(key)
            except Exception as e:
                self._degrade(e)
        self._mem.pop(key, None)


cache = Cache()
