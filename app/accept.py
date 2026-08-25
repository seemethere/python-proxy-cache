from __future__ import annotations

CONTENT_TYPE_JSON = "application/vnd.pypi.simple.v1+json"
CONTENT_TYPE_HTML = "text/html"


def want_json(accept: str | None) -> bool:
    """True when the client accepts the PEP 691 JSON media type.

    Honours an explicit ``q=0`` on the JSON type, which per RFC 9110 means "not
    acceptable" — a plain substring match would read that as a request for JSON.
    """
    if not accept:
        return False
    for part in accept.split(","):
        media_type, _, params = part.strip().partition(";")
        if media_type.strip() != CONTENT_TYPE_JSON:
            continue
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip().lower() != "q":
                continue
            try:
                return float(value.strip()) > 0
            except ValueError:
                return True
        return True
    return False
