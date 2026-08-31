"""Fake PyPI upstream: serves a Simple index plus the artifact bytes it links to.

Runs inside the compose network so we can assert on the whole path without
touching the real PyPI.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer

HOST = "fake-files:9100"

WHEEL_METADATA = b"Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n"


def _wheel_bytes() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as wheel:
        wheel.writestr("demo/__init__.py", b"# fake wheel payload\n" + b"x" * 4096)
        wheel.writestr("demo-1.0.dist-info/METADATA", WHEEL_METADATA)
    return output.getvalue()


WHEEL_BYTES = _wheel_bytes()
WHEEL_SHA256 = hashlib.sha256(WHEEL_BYTES).hexdigest()

# A body carrying every field group the model does NOT represent, so we can prove
# the passthrough path is not lossy end to end.
PROJECT_JSON = {
    "name": "demo",
    "versions": ["1.0", "2.0"],
    "files": [
        {
            "filename": "demo-1.0-py3-none-any.whl",
            "url": f"http://{HOST}/packages/demo-1.0-py3-none-any.whl",
            "hashes": {"sha256": WHEEL_SHA256},
            "requires-python": ">=3.9",
            "core-metadata": {"sha256": "deadbeef"},
            "size": len(WHEEL_BYTES),
            "upload-time": "2024-01-01T00:00:00Z",
            "provenance": f"http://{HOST}/packages/demo-1.0.provenance",
        }
    ],
    "meta": {"api-version": "1.1", "tracks": ["https://example.invalid/simple/demo/"]},
    "alternate-locations": ["https://mirror.invalid/simple/demo/"],
}

HTML_PROJECT = (
    "<!DOCTYPE html>\n<html><head>"
    '<meta name="pypi:repository-version" content="1.1"></head><body>\n'
    f'<a href="http://{HOST}/packages/legacy-1.0-py3-none-any.whl#sha256={WHEEL_SHA256}" '
    'data-requires-python="&gt;=3.8" data-custom-attr="preserve-me">'
    "legacy-1.0-py3-none-any.whl</a><br/>\n"
    "</body></html>"
)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/simple/demo/":
            body = json.dumps(PROJECT_JSON).encode()
            self._send(
                200,
                body,
                "application/vnd.pypi.simple.v1+json",
                {"ETag": '"demo-v1"', "Cache-Control": "public, max-age=30"},
            )
        elif path == "/simple/legacy/":
            self._send(200, HTML_PROJECT.encode(), "text/html")
        elif path == "/simple/missing/":
            self._send(404, b"Not Found", "text/plain")
        elif path == "/packages/demo-1.0-py3-none-any.whl.metadata":
            # The JSON project advertises an existing upstream sidecar.
            self._send(200, WHEEL_METADATA, "text/plain")
        elif path.startswith("/packages/") and path.endswith(".metadata"):
            # This legacy index has no sidecar, so the proxy must generate it
            # from the wheel rather than passing it through.
            self._send(404, b"upstream has no metadata sidecar", "text/plain")
        elif path.startswith("/packages/"):
            range_header = self.headers.get("Range")
            if range_header:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
                if not match:
                    self._send(416, b"", "text/plain")
                    return
                start_text, end_text = match.groups()
                if start_text:
                    start = int(start_text)
                    end = int(end_text) if end_text else len(WHEEL_BYTES) - 1
                else:
                    length = int(end_text)
                    start = max(0, len(WHEEL_BYTES) - length)
                    end = len(WHEEL_BYTES) - 1
                end = min(end, len(WHEEL_BYTES) - 1)
                if start >= len(WHEEL_BYTES) or start > end:
                    self._send(
                        416,
                        b"",
                        "text/plain",
                        {"Content-Range": f"bytes */{len(WHEEL_BYTES)}"},
                    )
                    return
                body = WHEEL_BYTES[start : end + 1]
                self._send(
                    206,
                    body,
                    "application/octet-stream",
                    {
                        "Accept-Ranges": "bytes",
                        "Content-Range": f"bytes {start}-{end}/{len(WHEEL_BYTES)}",
                    },
                )
            else:
                self._send(
                    200,
                    WHEEL_BYTES,
                    "application/octet-stream",
                    {"Accept-Ranges": "bytes"},
                )
        else:
            self._send(404, b"nope", "text/plain")

    def log_message(self, format: str, *args) -> None:
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 9100), Handler).serve_forever()
