from __future__ import annotations

import re

from bs4 import BeautifulSoup

from app.models import File, Project


def metadata_value_to_html(value: bool | str | dict[str, str] | None) -> str | None:
    """Render one PEP 658/714 HTML attribute value.

    HTML permits one hash, unlike the JSON mapping. Prefer SHA256 and otherwise
    use the first upstream-provided algorithm.
    """
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value:
        for algorithm in ("sha256", "sha384", "sha512", "blake2b", "sha1", "md5"):
            digest = value.get(algorithm)
            if digest:
                return f"{algorithm}={digest}"
    return None


def _parse_hashes(url: str) -> dict[str, str]:
    if "#" not in url:
        return {}
    frag = url.split("#", 1)[1]
    # PEP 503: #sha256=abc..., may have & or , separators
    out: dict[str, str] = {}
    # fragment can be sha256=xxx or md5=...&sha256=...
    for part in re.split(r"[&,]", frag):
        if "=" in part:
            k, v = part.split("=", 1)
            # only keep known hash names
            if k in ("sha256", "sha384", "sha512", "md5", "sha1", "blake2b"):
                out[k] = v
    return out


def _strip_fragment(url: str) -> str:
    return url.split("#", 1)[0]


def parse_simple_html(project_name: str, html: str) -> Project:
    soup = BeautifulSoup(html, "html.parser")
    files: list[File] = []
    for a in soup.find_all("a"):
        href = a.get("href")
        if not href:
            continue
        href = str(href) if not isinstance(href, str) else href
        filename = a.text.strip()
        # href may be relative or absolute
        hashes = _parse_hashes(href)
        url = _strip_fragment(href)

        # attributes per PEP 503 / 658 / 714
        requires_python = a.get("data-requires-python")
        if isinstance(requires_python, list):
            requires_python = ", ".join(str(x) for x in requires_python)
        elif requires_python is not None and not isinstance(requires_python, str):
            requires_python = str(requires_python)
        yanked = a.get("data-yanked")
        if isinstance(yanked, list):
            yanked = ", ".join(str(x) for x in yanked)
        elif yanked is not None and not isinstance(yanked, str):
            yanked = str(yanked)
        # data-dist-info-metadata / data-core-metadata can be "true", "false", or hash-like
        dim = a.get("data-dist-info-metadata")
        if isinstance(dim, list):
            dim = ", ".join(str(x) for x in dim)
        elif dim is not None and not isinstance(dim, str):
            dim = str(dim)
        cm = a.get("data-core-metadata")
        if isinstance(cm, list):
            cm = ", ".join(str(x) for x in cm)
        elif cm is not None and not isinstance(cm, str):
            cm = str(cm)

        # normalize booleans
        def _norm_meta(v):
            if v is None:
                return None
            if v.lower() == "true":
                return True
            if v.lower() == "false":
                return False
            # HTML uses ``hash-name=hex-digest`` while PEP 691 represents the
            # same value as an object. Normalise here so HTML -> JSON synthesis
            # emits the correct shape.
            if "=" in v:
                algorithm, digest = v.split("=", 1)
                if algorithm and digest:
                    return {algorithm: digest}
            return v  # hash value

        # data-yanked: "" or "true" or reason string. spec: absence = not yanked
        yanked_val = None
        if yanked is not None:
            yanked_val = True if yanked == "" or yanked.lower() == "true" else yanked

        files.append(
            File(
                filename=filename,
                url=url,
                hashes=hashes,
                requires_python=requires_python,
                yanked=yanked_val,
                dist_info_metadata=_norm_meta(dim) if dim is not None else None,
                core_metadata=_norm_meta(cm) if cm is not None else None,
            )
        )
    return Project(name=project_name, files=files)


def parse_simple_json(data: dict) -> Project:
    name = data.get("name", "")
    files: list[File] = []
    for f in data.get("files", []):
        # PEP 691 fields: filename, url, hashes, requires-python, yanked, dist-info-metadata, core-metadata
        yanked = f.get("yanked")
        # yanked can be bool or string
        files.append(
            File(
                filename=f.get("filename", ""),
                url=f.get("url", ""),
                hashes=f.get("hashes", {}),
                requires_python=f.get("requires-python"),
                yanked=yanked,
                dist_info_metadata=f.get("dist-info-metadata"),
                core_metadata=f.get("core-metadata"),
                size=f.get("size"),
                upload_time=f.get("upload-time"),
            )
        )
    return Project(name=name, files=files)


def model_to_json(project: Project) -> dict:
    files = []
    for f in project.files:
        entry: dict = {
            "filename": f.filename,
            "url": f.url,
            "hashes": f.hashes,
        }
        if f.requires_python is not None:
            entry["requires-python"] = f.requires_python
        if f.yanked is not None:
            entry["yanked"] = f.yanked
        # PEP 714 prefers core-metadata, keep both for compat if present
        if f.core_metadata is not None:
            entry["core-metadata"] = f.core_metadata
        if f.dist_info_metadata is not None:
            entry["dist-info-metadata"] = f.dist_info_metadata
        # if neither set, explicitly set core-metadata false so clients know (synthesized)
        if f.core_metadata is None and f.dist_info_metadata is None:
            entry["core-metadata"] = False
        if f.size is not None:
            entry["size"] = f.size
        if f.upload_time is not None:
            entry["upload-time"] = f.upload_time
        files.append(entry)
    return {
        "name": project.name,
        "files": files,
        "meta": {"api-version": "1.1"},
    }


def model_to_html(project: Project) -> str:
    lines = [
        "<!DOCTYPE html>",
        '<html><head><meta name="pypi:repository-version" content="1.1">',
        f"<title>Links for {project.name}</title></head><body>",
        f"<h1>Links for {project.name}</h1>",
    ]
    for f in project.files:
        # reconstruct fragment from hashes (prefer sha256)
        frag = ""
        if f.hashes:
            # join with &
            frag = "#" + "&".join(f"{k}={v}" for k, v in f.hashes.items())
        url = f.url + frag
        attrs = []
        if f.requires_python:
            attrs.append(f'data-requires-python="{_esc(f.requires_python)}"')
        if f.yanked is True:
            attrs.append('data-yanked=""')
        elif isinstance(f.yanked, str) and f.yanked:
            attrs.append(f'data-yanked="{_esc(f.yanked)}"')
        # core-metadata takes precedence per PEP 714
        core_metadata = metadata_value_to_html(f.core_metadata)
        dist_info_metadata = metadata_value_to_html(f.dist_info_metadata)
        if core_metadata is not None:
            attrs.append(f'data-core-metadata="{_esc(core_metadata)}"')
        elif dist_info_metadata is not None:
            attrs.append(f'data-dist-info-metadata="{_esc(dist_info_metadata)}"')
        else:
            # synthesized missing -> false
            if f.core_metadata is None and f.dist_info_metadata is None:
                attrs.append('data-core-metadata="false"')
        attr_str = (" " + " ".join(attrs)) if attrs else ""
        lines.append(f'<a href="{_esc(url)}"{attr_str}>{_esc(f.filename)}</a><br/>')
    lines.append("</body></html>")
    return "\n".join(lines)


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
