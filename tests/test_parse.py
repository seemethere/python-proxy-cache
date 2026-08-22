from app.parse import model_to_html, model_to_json, parse_simple_html, parse_simple_json

HTML_SAMPLE = """<!DOCTYPE html><html><body>
<a href="https://files.pythonhosted.org/packages/a/b/requests-2.31.0-py3-none-any.whl#sha256=abc123" data-requires-python="&gt;=3.7" data-core-metadata="true">requests-2.31.0-py3-none-any.whl</a><br/>
<a href="https://files.pythonhosted.org/packages/c/d/requests-2.31.0.tar.gz#sha256=def456">requests-2.31.0.tar.gz</a>
</body></html>"""


def test_html_to_json_synthesis():
    proj = parse_simple_html("requests", HTML_SAMPLE)
    assert len(proj.files) == 2
    assert proj.files[0].hashes["sha256"] == "abc123"
    assert proj.files[0].requires_python == ">=3.7"
    assert proj.files[0].core_metadata is True
    data = model_to_json(proj)
    assert data["name"] == "requests"
    assert data["files"][0]["hashes"]["sha256"] == "abc123"
    assert data["files"][0]["core-metadata"] is True
    assert data["meta"]["api-version"] == "1.1"


def test_json_to_html_synthesis():
    data = {
        "name": "urllib3",
        "files": [
            {
                "filename": "urllib3-2.0.0-py3-none-any.whl",
                "url": "https://files.pythonhosted.org/packages/x.whl",
                "hashes": {"sha256": "fff"},
                "requires-python": ">=3.8",
                "core-metadata": True,
            },
            {
                "filename": "urllib3-2.0.0.tar.gz",
                "url": "https://files.pythonhosted.org/packages/y.tar.gz",
                "hashes": {"sha256": "ggg"},
                "yanked": "security",
            },
        ],
        "meta": {"api-version": "1.1"},
    }
    proj = parse_simple_json(data)
    html = model_to_html(proj)
    assert "urllib3-2.0.0-py3-none-any.whl" in html
    assert 'data-core-metadata="true"' in html
    assert 'data-yanked="security"' in html
    assert "sha256=fff" in html


def test_roundtrip():
    proj = parse_simple_html("demo", HTML_SAMPLE)
    j = model_to_json(proj)
    proj2 = parse_simple_json(j)
    assert len(proj2.files) == len(proj.files)
    assert proj2.files[0].filename == proj.files[0].filename
