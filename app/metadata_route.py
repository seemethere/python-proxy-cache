from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from app.artifacts import host_allowed
from app.config import settings
from app.metadata import load_metadata_for_url

router = APIRouter()


@router.get("/artifacts/{host}/{path:path}.metadata")
async def wheel_metadata(host: str, path: str) -> Response:
    """Serve generated PEP 658/714 metadata at ``wheel-url.metadata``.

    nginx must route this more-specific suffix to FastAPI before its general
    ``/artifacts/`` upstream proxy location.
    """
    if not host_allowed(host) or not path.endswith(".whl"):
        raise HTTPException(status_code=404, detail="metadata not found")
    if any(part in ("", ".", "..") for part in path.split("/")):
        raise HTTPException(status_code=404, detail="metadata not found")

    artifact_url = f"/artifacts/{host}/{path}"
    stored = await load_metadata_for_url(artifact_url)
    if stored is None:
        raise HTTPException(status_code=404, detail="metadata not found")
    body, metadata_sha256 = stored
    return Response(
        content=body,
        media_type="text/plain; charset=UTF-8",
        headers={
            "Cache-Control": f"public, max-age={settings.metadata_cache_ttl_seconds}, immutable",
            "ETag": f'"sha256:{metadata_sha256}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
