"""The dashboard runs only code it ships.

A script or stylesheet loaded from another origin runs with the dashboard's
own authority (it can switch accounts and start OAuth flows), and a CDN URL
can change under a fixed version or none at all. Every page loads its
scripts and styles from its own origin, and every vendored file matches the
bytes recorded here. To update a vendored file, follow
jacked/data/web/js/vendor/LICENSES.md and change its pin below.
"""

import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "jacked" / "data" / "web"
VENDOR = WEB / "js" / "vendor"

PINNED = {
    "tailwindcss-3.4.17.js": "176e894661aa9cdc9a5cba6c720044cbbf7b8bd80d1c9a142a7c24b1b6c50d15",
    "sweetalert2-11.26.25.all.min.js": "4e86f0e22e4771b5b8aac24c613c661806a678b1afa284d03cf6ad03d3e21a0a",
    "chart.umd.min.js": "48444a82d4edcb5bec0f1965faacdde18d9c17db3063d042abada2f705c9f54a",
    "chartjs-chart-matrix.min.js": "88391c688cc3ed7942109ee3e6a47a2e50238eaa3b29850f29df762696562766",
}


class _Loads(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("src"):
            self.urls.append(attrs["src"])
        if tag == "link" and attrs.get("href") and "stylesheet" in (attrs.get("rel") or ""):
            self.urls.append(attrs["href"])
        if tag in {"img", "iframe"} and attrs.get("src"):
            self.urls.append(attrs["src"])


@pytest.mark.parametrize("page", sorted(WEB.glob("*.html")), ids=lambda p: p.name)
def test_a_page_loads_nothing_from_another_origin(page):
    parser = _Loads()
    parser.feed(page.read_text(encoding="utf-8"))
    foreign = [u for u in parser.urls if re.match(r"^(https?:)?//", u)]
    assert not foreign, f"{page.name} loads {foreign}"


def test_scripts_never_load_code_from_another_origin():
    """Dynamic loads too (analytics injects Chart.js with s.src = ...)."""
    pattern = re.compile(r"""(?:\.src\s*=|import\(|importScripts\()\s*['"`](?:https?:)?//""")
    offenders = [
        str(path.relative_to(WEB))
        for path in sorted((WEB / "js").rglob("*.js"))
        if VENDOR not in path.parents and pattern.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders


def test_every_vendored_file_is_pinned_and_licensed():
    shipped = {p.name for p in VENDOR.glob("*.js")}
    assert shipped == set(PINNED), "pin every vendored file here (and nothing else)"
    licenses = (VENDOR / "LICENSES.md").read_text(encoding="utf-8")
    for name, digest in PINNED.items():
        assert hashlib.sha256((VENDOR / name).read_bytes()).hexdigest() == digest, name
        assert f"`{name}`" in licenses, f"{name} is missing from LICENSES.md"


def test_the_server_serves_the_vendored_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from fastapi.testclient import TestClient

    from jacked.api.main import app

    client = TestClient(app)
    index = client.get("/")
    assert index.status_code == 200
    for src in re.findall(r'<script src="(/js/vendor/[^"]+)"', index.text):
        response = client.get(src)
        assert response.status_code == 200, src
        name = src.rsplit("/", 1)[-1]
        assert hashlib.sha256(response.content).hexdigest() == PINNED[name]
