from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import httpx
import pytest
from httpx import ASGITransport, AsyncClient, Response

import app.main as main_mod
from app.cache import cache
from app.main import app


@dataclass
class UpstreamRecorder:
    """Records upstream httpx.get calls and returns a canned Response."""

    handler: Callable[..., Response]
    calls: list[dict] = field(default_factory=list)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    async def get(self, url, headers=None):
        self.calls.append({"url": str(url), "headers": dict(headers or {})})
        result = self.handler(url, headers)
        if isinstance(result, Response):
            return result
        return await result  # async handler


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    cache._mem.clear()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    cache._mem.clear()


@pytest.fixture
def mock_upstream(monkeypatch: pytest.MonkeyPatch):
    """Install a recorder as app.main.http_client.get. Call with a handler factory."""

    if main_mod.http_client is None:
        main_mod.http_client = httpx.AsyncClient()

    orig_get = main_mod.http_client.get
    recorder_box: dict[str, UpstreamRecorder] = {}

    def install(handler: Callable[..., Response]) -> UpstreamRecorder:
        rec = UpstreamRecorder(handler=handler)
        recorder_box["rec"] = rec
        monkeypatch.setattr(main_mod.http_client, "get", rec.get)
        return rec

    yield install

    monkeypatch.setattr(main_mod.http_client, "get", orig_get)
    cache._mem.clear()
