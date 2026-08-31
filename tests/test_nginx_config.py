from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIGS = (ROOT / "nginx/nginx.conf", ROOT / "tests/integration/nginx.it.conf")


def _location(config: str, name: str) -> str:
    start = config.index(f"location {name} {{")
    end = config.index("\n    }", start)
    return config[start:end]


@pytest.mark.parametrize("path", CONFIGS)
def test_metadata_fallback_does_not_cache_not_found(path: Path):
    fallback = _location(path.read_text(), "@artifact_metadata_upstream")

    assert "proxy_cache packages;" in fallback
    assert 'proxy_cache_key "metadata:$uri$is_args$args";' in fallback
    assert "proxy_cache_valid 404" not in fallback


@pytest.mark.parametrize("path", CONFIGS)
@pytest.mark.parametrize(
    "location",
    (
        "@artifact_metadata_upstream",
        "~ ^/artifacts/(?<artifact_host>[^/]+)(?<artifact_uri>/.*)$",
    ),
)
def test_artifact_cache_ignores_origin_freshness_headers(path: Path, location: str):
    artifact = _location(path.read_text(), location)

    assert "proxy_ignore_headers Cache-Control Expires;" in artifact
    # Set-Cookie still prevents caching; ignoring it without also suppressing
    # the response header could replay one user's cookie to another client.
    assert "Set-Cookie" not in artifact
