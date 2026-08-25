from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

from app.config import settings
from app.models import File, Project

_ATTR_RE = re.compile(r"(?P<attr>href)\s*=\s*(?P<quote>[\"'])(?P<url>[^\"']*)(?P=quote)")


_DEFAULT_PORTS = {"http": 80, "https": 443}


def authority_of(parsed) -> str:
    """host[:port] for a parsed URL, omitting the port when it is the default.

    urlparse().hostname drops the port entirely, which would rewrite an internal
    index on nexus.internal:8081 to /artifacts/nexus.internal/ — unreachable.
    """
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port is not None and port != _DEFAULT_PORTS.get(parsed.scheme):
        return f"{host}:{port}"
    return host


def host_allowed(host: str) -> bool:
    """Exact authority match against the allowlist.

    Deliberately strict about ports. Both sides are normalised by authority_of,
    so a bare allowlist entry carries the scheme's default port implicitly and
    matches only that; an upstream link cannot aim the proxy at some other port
    on a host that was only meant to be reachable on its normal one.
    """
    return host.lower() in settings.artifact_hosts()


def rewrite_file_url(url: str, *, base: str | None = None) -> str:
    """Rewrite an absolute (or base-resolved) file URL to /artifacts/<host>/<path>.

    Hosts not on the allowlist are left unchanged (clients hit them directly;
    nginx must not become an open proxy for arbitrary hosts via our Simple links).
    Fragment (hashes) and query are preserved.
    """
    if not url:
        return url
    if url.startswith("/artifacts/"):
        return url
    resolved = urljoin(base or settings.upstream_files_url.rstrip("/") + "/", url)
    parsed = urlparse(resolved)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return url
    host = authority_of(parsed)
    if not host_allowed(host):
        return url
    # Drop userinfo; keep path/query/fragment
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    artifact = f"/artifacts/{host}{path}"
    if parsed.query:
        artifact += f"?{parsed.query}"
    if parsed.fragment:
        artifact += f"#{parsed.fragment}"
    return artifact


def rewrite_project_urls(project: Project) -> Project:
    """Rewrite URLs on a parsed Project (used on the synthesis path)."""
    files: list[File] = []
    for f in project.files:
        files.append(
            File(
                filename=f.filename,
                url=rewrite_file_url(f.url),
                hashes=f.hashes,
                requires_python=f.requires_python,
                yanked=f.yanked,
                dist_info_metadata=f.dist_info_metadata,
                core_metadata=f.core_metadata,
                size=f.size,
                upload_time=f.upload_time,
            )
        )
    return Project(name=project.name, files=files)


def rewrite_json_body(body: str) -> str:
    """Rewrite file URLs in a PEP 691 body, preserving every other field.

    Mutates urls on the decoded document rather than round-tripping through
    Project/File, which carries only the fields we model — a round trip drops
    PEP 700 ``versions``, PEP 708 ``meta.tracks`` / ``alternate-locations`` and
    PEP 740 ``provenance``, and rewrites ``meta`` to a hardcoded api-version.
    """
    data = json.loads(body)
    files = data.get("files")
    if isinstance(files, list):
        for entry in files:
            if isinstance(entry, dict) and isinstance(entry.get("url"), str):
                entry["url"] = rewrite_file_url(entry["url"])
    return json.dumps(data)


def rewrite_html_body(body: str) -> str:
    """Rewrite anchor hrefs in a PEP 503 body, preserving the rest of the markup.

    Uses a targeted attribute substitution so upstream markup, attribute order and
    any element we do not model survive untouched.
    """

    def _sub(m: re.Match[str]) -> str:
        rewritten = rewrite_file_url(m.group("url"))
        if rewritten == m.group("url"):
            return m.group(0)
        quote = m.group("quote")
        return f"{m.group('attr')}={quote}{rewritten}{quote}"

    return _ATTR_RE.sub(_sub, body)


def rewrite_simple_body(body: str, *, is_json: bool, project_name: str = "") -> str:
    """Rewrite file URLs inside a Simple API HTML or JSON body, losslessly."""
    return rewrite_json_body(body) if is_json else rewrite_html_body(body)


__all__ = [
    "host_allowed",
    "rewrite_file_url",
    "rewrite_html_body",
    "rewrite_json_body",
    "rewrite_project_urls",
    "rewrite_simple_body",
]
