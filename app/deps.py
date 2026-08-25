from __future__ import annotations

import httpx
from fastapi import HTTPException


def get_http_client() -> httpx.AsyncClient:
    """Lazy access so handlers can live outside main without import cycles.

    Tests continue to patch ``app.main.http_client``.
    """
    from app import main as app_main

    if app_main.http_client is None:
        raise HTTPException(status_code=502, detail="upstream client not ready")
    return app_main.http_client
