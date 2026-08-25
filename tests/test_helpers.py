import pytest

from app.accept import CONTENT_TYPE_JSON, want_json
from app.config import settings
from app.ttl import effective_project_ttl, parse_max_age


@pytest.mark.parametrize(
    ("accept", "expected"),
    [
        (None, False),
        ("", False),
        ("text/html", False),
        (CONTENT_TYPE_JSON, True),
        (f"text/html, {CONTENT_TYPE_JSON}", True),
        (f"{CONTENT_TYPE_JSON};q=0.9", True),
    ],
)
def test_want_json(accept, expected):
    assert want_json(accept) is expected


@pytest.mark.parametrize(
    ("cc", "expected"),
    [
        (None, None),
        ("", None),
        ("public, max-age=600", 600),
        ("max-age=60, must-revalidate", 60),
        ("no-cache", None),
        ("max-age=abc", None),
    ],
)
def test_parse_max_age(cc, expected):
    assert parse_max_age(cc) == expected


def test_effective_project_ttl_caps_only():
    base = settings.cache_project_ttl_seconds
    assert effective_project_ttl(None) == base
    assert effective_project_ttl({}) == base
    # shorter max-age wins
    assert effective_project_ttl({"cache-control": "max-age=5"}) == 5
    # longer max-age does not extend
    assert effective_project_ttl({"Cache-Control": f"max-age={base + 100}"}) == base
