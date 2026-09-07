from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

from app.config import settings
from app.models import File, Project

_ATTR_RE = re.compile(
    r"(?P<attr>href)\s*=\s*(?P<quote>[\"'])(?P<url>[^\"']*)(?P=quote)", re.IGNORECASE
)
_ANCHOR_RE = re.compile(r"<a\b[^>]*>", re.IGNORECASE)
_METADATA_ATTR_RE = re.compile(
    r"data-(?:core|dist-info)-metadata\s*=\s*(?P<quote>[\"'])(?P<value>[^\"']*)(?P=quote)",
    re.IGNORECASE,
)


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
                url=(
                    rewrite_file_url(f.url)
                    if settings.rewrite_artifact_urls or _file_advertises_metadata(f)
                    else f.url
                ),
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
    if (
        not settings.rewrite_artifact_urls
        and '"core-metadata"' not in body
        and '"dist-info-metadata"' not in body
    ):
        return body
    data = json.loads(body)
    files = data.get("files")
    if isinstance(files, list):
        for entry in files:
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("url"), str)
                and (settings.rewrite_artifact_urls or _json_entry_advertises_metadata(entry))
            ):
                entry["url"] = rewrite_file_url(entry["url"])
    return json.dumps(data)


def rewrite_html_body(body: str) -> str:
    """Rewrite anchor hrefs in a PEP 503 body, preserving the rest of the markup.

    Uses a targeted attribute substitution so upstream markup, attribute order and
    any element we do not model survive untouched.
    """

    if not settings.rewrite_artifact_urls and _METADATA_ATTR_RE.search(body) is None:
        return body

    def _rewrite_href(m: re.Match[str]) -> str:
        rewritten = rewrite_file_url(m.group("url"))
        if rewritten == m.group("url"):
            return m.group(0)
        quote = m.group("quote")
        return f"{m.group('attr')}={quote}{rewritten}{quote}"

    def _rewrite_anchor(m: re.Match[str]) -> str:
        anchor = m.group(0)
        if not settings.rewrite_artifact_urls:
            advertised = any(
                metadata.group("value").strip().lower() not in ("", "false")
                for metadata in _METADATA_ATTR_RE.finditer(anchor)
            )
            if not advertised:
                return anchor
        return _ATTR_RE.sub(_rewrite_href, anchor, count=1)

    return _ANCHOR_RE.sub(_rewrite_anchor, body)


def _metadata_value_advertised(value: object) -> bool:
    return value is True or isinstance(value, (str, dict))


def _file_advertises_metadata(file: File) -> bool:
    return _metadata_value_advertised(file.core_metadata) or _metadata_value_advertised(
        file.dist_info_metadata
    )


def _json_entry_advertises_metadata(entry: dict) -> bool:
    return _metadata_value_advertised(entry.get("core-metadata")) or _metadata_value_advertised(
        entry.get("dist-info-metadata")
    )


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
