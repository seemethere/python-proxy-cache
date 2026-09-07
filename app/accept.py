from __future__ import annotations

CONTENT_TYPE_JSON = "application/vnd.pypi.simple.v1+json"
CONTENT_TYPE_HTML = "text/html"


def _quality(accept: str, media_type: str) -> float | None:
    major_type = media_type.split("/", 1)[0]
    best_specificity = -1
    best_quality = 0.0
    for part in accept.split(","):
        candidate, _, params = part.strip().partition(";")
        candidate = candidate.strip().lower()
        if candidate == media_type:
            specificity = 2
        elif candidate == f"{major_type}/*":
            specificity = 1
        elif candidate == "*/*":
            specificity = 0
        else:
            continue
        quality = 1.0
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip().lower() == "q":
                try:
                    quality = float(value.strip())
                except ValueError:
                    quality = 1.0
        if specificity > best_specificity:
            best_specificity = specificity
            best_quality = quality
        elif specificity == best_specificity:
            best_quality = max(best_quality, quality)
    return best_quality if best_specificity >= 0 else None


def accepts_html(accept: str | None) -> bool:
    """Whether an HTML Simple response is acceptable to the client."""
    return not accept or (_quality(accept, CONTENT_TYPE_HTML) or 0) > 0


def want_json(accept: str | None) -> bool:
    """True when the client accepts the PEP 691 JSON media type.

    Honours an explicit ``q=0`` on the JSON type, which per RFC 9110 means "not
    acceptable" — a plain substring match would read that as a request for JSON.
    """
    if not accept:
        return False
    for part in accept.split(","):
        media_type, _, params = part.strip().partition(";")
        # media types are case-insensitive (RFC 9110 8.3.1)
        if media_type.strip().lower() != CONTENT_TYPE_JSON:
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
