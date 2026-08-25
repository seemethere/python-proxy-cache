from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class KeyLocker:
    """In-process per-key locks for cache-miss singleflight.

    Multi-replica coherence still relies on Redis + nginx proxy_cache_lock.
    Locks are removed when the last waiter releases so the map cannot grow
    without bound from unique project names.
    """

    def __init__(self) -> None:
        self._meta = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}
        self._waiters: dict[str, int] = {}

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        async with self._meta:
            lock = self._locks.setdefault(key, asyncio.Lock())
            self._waiters[key] = self._waiters.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            async with self._meta:
                left = self._waiters.get(key, 1) - 1
                if left <= 0:
                    self._waiters.pop(key, None)
                    self._locks.pop(key, None)
                else:
                    self._waiters[key] = left


miss_locks = KeyLocker()
