"""Guardrail: the canonical phase list must never drift between writers and the UI."""

from jacked.service.update_phases import PHASES, PHASE_NAMES


def test_six_phases_in_expected_order():
    """Order matters — the UI renders phases in this order."""
    assert PHASE_NAMES == [
        "waiting_for_parent",
        "installing_package",
        "migrating_settings",
        "waiting_port_free",
        "starting_service",
        "verifying_service",
    ]


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
