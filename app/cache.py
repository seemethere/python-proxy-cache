from __future__ import annotations

import time

try:
    import redis.asyncio as redis

    HAS_REDIS = True
except ImportError:
    HAS_REDIS = False

from app.config import settings


class Cache:
    def __init__(self, redis_url: str | None = None):
        self._redis = None
        self._mem: dict[str, tuple[float, str]] = {}
        self.url = redis_url or settings.redis_url
        if HAS_REDIS:
            try:
                self._redis = redis.from_url(
                    self.url, decode_responses=True, socket_connect_timeout=1
                )
            except Exception:  # fallback to in-memory on any redis URL/connection error
                self._redis = None

    async def ping(self) -> bool:
        if self._redis is None:
            return False
        try:
            await self._redis.ping()
            return True
        except Exception:  # any redis failure means not ok
            return False

    async def get(self, key: str) -> str | None:
        if self._redis:
            try:
                v = await self._redis.get(key)
                if v is not None:
                    if isinstance(v, bytes):
                        return v.decode()
                    return str(v)
            except Exception:  # fallback to in-memory cache
                pass
        # fallback mem
        if key in self._mem:
            exp, val = self._mem[key]
            if exp > time.time():
                return val
            else:
                del self._mem[key]
        return None

    async def setex(self, key: str, ttl: int, value: str):
        if self._redis:
            try:
                await self._redis.setex(key, ttl, value)  # type: ignore[attr-defined]
                return
            except Exception:  # fallback to in-memory cache
                pass
        self._mem[key] = (time.time() + ttl, value)


cache = Cache()
