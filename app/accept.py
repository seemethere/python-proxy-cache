from __future__ import annotations

CONTENT_TYPE_JSON = "application/vnd.pypi.simple.v1+json"
CONTENT_TYPE_HTML = "text/html"


def want_json(accept: str | None) -> bool:
    if not accept:
        return False
    return CONTENT_TYPE_JSON in accept
