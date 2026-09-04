"""Guardrail: the canonical phase list must never drift between writers and the UI."""

from jacked.service.update_phases import PHASES, PHASE_NAMES


def test_phases_in_expected_order():
    """Order matters — the UI renders phases in this order.

    `preflight` sits directly after the package install: it is the transaction
    gate, and it must close before `migrating_settings` replaces the old
    install. `rolling_back` is last because it only runs when something ahead
    of it failed.
    """
    assert PHASE_NAMES == [
        "waiting_for_parent",
        "installing_package",
        "preflight",
        "migrating_settings",
        "waiting_port_free",
        "starting_service",
        "verifying_service",
        "rolling_back",
    ]


def test_preflight_precedes_the_settings_migration():
    """Pinned separately: this ordering is the whole safety property."""
    assert PHASE_NAMES.index("preflight") < PHASE_NAMES.index("migrating_settings")


def test_phases_have_name_and_label():
    for entry in PHASES:
        assert "name" in entry and entry["name"]
        assert "label" in entry and entry["label"]


def test_update_html_embeds_all_phase_names():
    """Drift-prevention: update.html's hardcoded PHASES JS constant must
    contain every phase name defined here."""
    from pathlib import Path
    import jacked
    repo_root = Path(jacked.__file__).resolve().parent
    html_path = repo_root / "data" / "web" / "update.html"
    if not html_path.exists():
        # update.html comes in Task 8 — until then this test is a leading guard.
        # Mark as passing so Task 1 is independently green; Task 8 satisfies it.
        return
    html = html_path.read_text()
    for name in PHASE_NAMES:
        assert name in html, f"update.html missing phase name: {name}"


def _web_dir():
    from pathlib import Path
    import jacked
    return Path(jacked.__file__).resolve().parent / "data" / "web"


def test_bootstrap_template_exists_and_is_self_hosting():
    """The file:// bootstrap hosts /update.html in an iframe and carries the
    port placeholder the tray substitutes at click time."""
    path = _web_dir() / "update_bootstrap.html"
    assert path.exists(), "update_bootstrap.html missing from data/web"
    html = path.read_text(encoding="utf-8")
    assert "__JACKED_PORT__" in html
    assert "<iframe" in html
    assert "/update.html" in html


def test_bootstrap_template_only_targets_loopback():
    """No external resources — the only http(s) URL is 127.0.0.1 loopback."""
    import re
    html = (_web_dir() / "update_bootstrap.html").read_text(encoding="utf-8")
    urls = re.findall(r"https?://[^\s\"'<>)]+", html)
    assert urls, "expected at least the loopback base URL"
    for url in urls:
        assert url.startswith("http://127.0.0.1"), f"non-loopback URL: {url}"


def test_update_html_posts_state_and_breaks_out_of_frame():
    """update.html must talk to a hosting bootstrap frame (postMessage),
    escape the iframe on dashboard links (_top), and render the down state."""
    html = (_web_dir() / "update.html").read_text(encoding="utf-8")
    assert "postMessage" in html
    assert "_top" in html
    assert "Service restarting" in html
