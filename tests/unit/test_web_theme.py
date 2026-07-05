"""Guardrail: the America 250 color theme must stay wired across CSS + HTML + JS.

The theme is presentation-only — semantic fill classes (green/yellow/red) are
load-bearing and untouched — so these are content-drift checks, not behavior
tests. If someone renames the html class, the localStorage key, or drops the
flag markup, the surfaces silently disagree; these catch that.
"""

from pathlib import Path

import jacked

_WEB = Path(jacked.__file__).resolve().parent / "data" / "web"


def _read(*parts):
    return (_WEB.joinpath(*parts)).read_text(encoding="utf-8")


def test_style_css_has_theme_class_and_fill_overrides():
    css = _read("css", "style.css")
    assert "theme-america250" in css
    for color in ("green", "yellow", "red"):
        selector = f"html.theme-america250 .usage-bar .fill.{color}"
        assert selector in css, f"style.css missing themed fill selector: {selector}"


def test_early_theme_snippet_in_both_pages():
    """The flash-preventing <head> snippet reads jacked_color_theme in both."""
    for page in ("index.html", "panel.html"):
        assert "jacked_color_theme" in _read(page), f"{page} missing early-theme key"


def test_settings_js_exposes_both_theme_values():
    js = _read("js", "components", "settings.js")
    assert "jacked_color_theme" in js
    assert "america250" in js
    assert "classic" in js


def test_usage_js_checks_theme_class():
    """The percent-label helper gates on the html class."""
    assert "theme-america250" in _read("js", "components", "usage.js")


def test_index_html_has_flag_badge_markup():
    html = _read("index.html")
    assert "america-250-badge" in html
    assert "3C3B6E" in html  # blue canton fill of the inline SVG flag
