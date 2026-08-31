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
