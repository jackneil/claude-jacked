"""Static contracts for evidence-qualified session rendering."""

from pathlib import Path


SESSIONS_JS = (
    Path(__file__).resolve().parents[2]
    / "jacked"
    / "data"
    / "web"
    / "js"
    / "components"
    / "sessions.js"
)
STYLE_CSS = SESSIONS_JS.parents[2] / "css" / "style.css"


def _source() -> str:
    return SESSIONS_JS.read_text(encoding="utf-8")


def test_session_ui_names_each_evidence_axis():
    source = _source()
    for label in (
        "Started as",
        "Observed configuration",
        "Pending next activity",
        "Pinned target",
        "Credential conflict",
        "Runtime unverified",
    ):
        assert label in source


def test_pinned_label_requires_scoped_launch_evidence():
    source = _source()
    pinned = source.index("Pinned target")
    nearby = source[max(0, pinned - 400) : pinned]
    assert "scope === 'scoped'" in nearby
    assert "evidence === 'launch_binding'" in nearby


def test_runtime_is_unverified_by_default():
    source = _source()
    assert "const runtime = _sessionIdentityLabel(s.runtime_verified);" in source
    assert "if (truth.runtime)" in source
    assert "Runtime unverified" in source


def test_session_ui_does_not_call_legacy_identity_sources():
    source = _source()
    assert ".claude.json" not in source
    assert ".credentials.json" not in source
    assert "accessToken" not in source


def test_unknown_session_bucket_has_a_visible_section():
    source = _source()
    assert "function renderUnknownSessions()" in source
    assert ".activeSessions || {}).unknown" in source
    assert "Sessions with unknown account" in source


def test_evidence_pills_wrap_instead_of_clipping_truth_labels():
    source = _source()
    css = STYLE_CSS.read_text(encoding="utf-8")
    assert "session-evidence-pill" in source
    assert ".active-repo-tag.session-evidence-pill" in css
    assert "white-space: normal" in css
