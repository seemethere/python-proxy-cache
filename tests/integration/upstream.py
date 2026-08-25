"""Fake PyPI upstream: serves a Simple index plus the artifact bytes it links to.

Runs inside the compose network so we can assert on the whole path without
touching the real PyPI.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

HOST = "fake-files:9100"

WHEEL_BYTES = b"PK\x03\x04 fake wheel payload " + b"x" * 4096

# A body carrying every field group the model does NOT represent, so we can prove
# the passthrough path is not lossy end to end.
PROJECT_JSON = {
    "name": "demo",
    "versions": ["1.0", "2.0"],
    "files": [
        {
            "filename": "demo-1.0-py3-none-any.whl",
            "url": f"http://{HOST}/packages/demo-1.0-py3-none-any.whl",
            "hashes": {"sha256": "abc123"},
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
    f'<a href="http://{HOST}/packages/legacy-1.0-py3-none-any.whl#sha256=def456" '
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
        elif path.startswith("/packages/") and path.endswith(".metadata"):
            self._send(200, b"Metadata-Version: 2.1\n", "text/plain")
        elif path.startswith("/packages/"):
            self._send(200, WHEEL_BYTES, "application/octet-stream")
        else:
            self._send(404, b"nope", "text/plain")

    def log_message(self, format: str, *args) -> None:
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 9100), Handler).serve_forever()
