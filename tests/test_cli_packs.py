"""CLI wiring for skill packs: `install --packs`, the `jacked packs` group, and
uninstall removal.

Every `jacked.packs` function is replaced with an in-memory fake (autospec-ish
closures that record their calls) so no test touches npx, the network, or a real
home. The heavy `install` internals (`_run_install`, the required-plugin warning)
are no-op'd, and the two external side-effect points the full `uninstall` path
hits (Codex uninstall, Chrome DevTools MCP) are neutralized per-test.
"""
import json
from io import StringIO
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from rich.console import Console

import jacked.cli as cli
from jacked import packs as packs_mod
from jacked.cli import main

# --------------------------------------------------------------------------- #
# Fixture packs (real Pack dataclass so attribute access matches production)
# --------------------------------------------------------------------------- #

MARKETING = packs_mod.Pack(
    name="marketing",
    display_name="Marketing Skills",
    description="Curated marketing skills from upstream, installed live via the skills CLI.",
    source="coreyhaines31/marketingskills",
    homepage="https://github.com/coreyhaines31/marketingskills",
    skills=("ads", "seo", "copywriting"),
)
DESIGN = packs_mod.Pack(
    name="design-extras",
    display_name="Design Extras",
    description="Emil Kowalski's improve-animations skill.",
    source="emilkowalski/skills",
    homepage="https://github.com/emilkowalski/skills",
    skills=("improve-animations",),
)
REGISTRY = {"marketing": MARKETING, "design-extras": DESIGN}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fake JACKED_HOME + a wide, buffered console + no-op heavy install steps."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("JACKED_HOME", str(home))

    buf = StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, width=200, highlight=False))

    # `install` does a lot of real filesystem/plugin work we don't exercise here.
    monkeypatch.setattr(cli, "_run_install", lambda **k: None)
    monkeypatch.setattr(cli, "_warn_required_plugins_missing", lambda *a, **k: None)

    return SimpleNamespace(home=home, buf=buf, monkeypatch=monkeypatch)


def _fake_packs(
    monkeypatch,
    *,
    registry=REGISTRY,
    enabled_before=None,
    npx="/usr/bin/npx",
    install_result=None,
    update_result=None,
    remove_result=None,
    status=None,
):
    """Replace every jacked.packs entrypoint with a recording fake.

    Returns a namespace of call records. ``*_result`` may be a PackOpResult or a
    callable(pack)->PackOpResult; ``status`` similarly for pack_status.
    """
    calls = SimpleNamespace(
        set_enabled=[], install_pack=[], update_packs=[], remove_pack=[],
        pack_status=[], order=[],
    )
    state = {"enabled": list(enabled_before or [])}

    def load_registry(data_root):
        return dict(registry)

    def set_enabled(home, name, enabled):
        calls.set_enabled.append((str(home), name, enabled))
        calls.order.append(("set_enabled", name, enabled))
        if enabled and name not in state["enabled"]:
            state["enabled"].append(name)
        elif not enabled and name in state["enabled"]:
            state["enabled"].remove(name)

    def enabled_pack_names(home):
        return sorted(state["enabled"])

    def find_npx():
        return npx

    def install_pack(pack, home, *, include_codex, timeout=600):
        calls.install_pack.append(SimpleNamespace(pack=pack, include_codex=include_codex))
        calls.order.append(("install_pack", pack.name))
        if install_result is not None:
            return install_result(pack) if callable(install_result) else install_result
        return packs_mod.PackOpResult(
            ok=True,
            installed=list(pack.skills),
            message=f"Installed {len(pack.skills)} skill(s) for pack '{pack.display_name}'.",
        )

    def update_packs(pks, home, *, include_codex, timeout=600):
        calls.update_packs.append(
            SimpleNamespace(packs=list(pks), include_codex=include_codex)
        )
        calls.order.append(("update_packs", [p.name for p in pks]))
        if update_result is not None:
            return update_result
        # Mirror the real contract: aggregate fields PLUS a per_pack dict of
        # per-pack PackOpResults keyed by pack name. The container is a plain
        # namespace so tests don't depend on the PackOpResult dataclass having
        # gained `per_pack` yet (a sibling agent is adding that field).
        per = {
            p.name: packs_mod.PackOpResult(
                ok=True,
                installed=list(p.skills),
                message=f"Updated pack '{p.display_name}'.",
            )
            for p in pks
        }
        return SimpleNamespace(
            ok=True,
            installed=[s for p in pks for s in p.skills],
            missing=[],
            message="Updated skill packs.",
            per_pack=per,
        )

    def remove_pack(pack, home, *, timeout=300):
        calls.remove_pack.append(SimpleNamespace(pack=pack))
        calls.order.append(("remove_pack", pack.name))
        if remove_result is not None:
            return remove_result(pack) if callable(remove_result) else remove_result
        return packs_mod.PackOpResult(
            ok=True,
            removed=list(pack.skills),
            message=f"Removed {len(pack.skills)} skill(s) for pack '{pack.display_name}'.",
        )

    def pack_status(pack, home):
        calls.pack_status.append(pack.name)
        if status is not None:
            return status(pack) if callable(status) else status
        return {
            "name": pack.name,
            "installed_count": len(pack.skills),
            "total": len(pack.skills),
            "skills": [{"name": s, "installed": True} for s in pack.skills],
        }

    for fn in (
        load_registry,
        set_enabled,
        enabled_pack_names,
        find_npx,
        install_pack,
        update_packs,
        remove_pack,
        pack_status,
    ):
        monkeypatch.setattr(packs_mod, fn.__name__, fn)
    return calls


def _stub_codex(monkeypatch, *, present=False):
    """Neutralize the Codex install probe (and its heavy install) for install tests."""
    import jacked.codex.installer as cdx

    monkeypatch.setattr(cdx, "codex_present", lambda *a, **k: present)
    monkeypatch.setattr(cdx, "install_codex", lambda *a, **k: None)


# --------------------------------------------------------------------------- #
# install --packs
# --------------------------------------------------------------------------- #

def test_install_unknown_pack_exits_before_state_mutation(env):
    calls = _fake_packs(env.monkeypatch)
    r = CliRunner().invoke(main, ["install", "--no-codex", "--packs", "bogus"])
    assert r.exit_code == 1
    # Nothing was enabled/installed — validation happened before any state write.
    assert calls.set_enabled == []
    assert calls.install_pack == []
    out = env.buf.getvalue()
    assert "Unknown skill pack" in out
    assert "marketing" in out and "design-extras" in out


@pytest.mark.parametrize(
    "args, present, expected",
    [
        (["install", "--packs", "marketing"], True, True),
        (["install", "--packs", "marketing", "--no-codex"], True, False),
    ],
)
def test_install_packs_mirrors_codex_detection(env, args, present, expected):
    calls = _fake_packs(env.monkeypatch)
    _stub_codex(env.monkeypatch, present=present)
    r = CliRunner().invoke(main, args)
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    assert any(c[1] == "marketing" and c[2] is True for c in calls.set_enabled)
    assert len(calls.install_pack) == 1
    assert calls.install_pack[0].include_codex is expected
    assert "[OK] Pack 'marketing'" in env.buf.getvalue()


def test_install_previously_enabled_batched_update(env):
    calls = _fake_packs(env.monkeypatch, enabled_before=["marketing"])
    r = CliRunner().invoke(main, ["install", "--no-codex"])
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    # Already-enabled packs refresh through ONE update call, never install_pack.
    assert calls.install_pack == []
    assert len(calls.update_packs) == 1
    assert [p.name for p in calls.update_packs[0].packs] == ["marketing"]
    assert "[OK] Pack 'marketing'" in env.buf.getvalue()


def test_install_npx_missing_warns_and_exits_zero(env):
    calls = _fake_packs(env.monkeypatch, enabled_before=["marketing"], npx=None)
    r = CliRunner().invoke(main, ["install", "--no-codex"])
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    assert calls.install_pack == []
    assert calls.update_packs == []
    assert "npx not found" in env.buf.getvalue()


def test_install_json_includes_packs_record(env):
    _fake_packs(env.monkeypatch)
    r = CliRunner().invoke(
        main, ["install", "--no-codex", "--json", "--packs", "marketing"]
    )
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    line = [ln for ln in r.output.splitlines() if ln.strip()][-1]
    rec = json.loads(line)
    assert "packs" in rec
    entry = rec["packs"]["marketing"]
    assert entry["ok"] is True
    assert entry["installed"] == len(MARKETING.skills)
    assert entry["missing"] == []
    assert isinstance(entry["message"], str)


# --------------------------------------------------------------------------- #
# jacked packs group
# --------------------------------------------------------------------------- #

def test_packs_list_renders_both(env):
    _fake_packs(env.monkeypatch, enabled_before=["marketing"])
    r = CliRunner().invoke(main, ["packs", "list"])
    assert r.exit_code == 0
    out = env.buf.getvalue()
    assert "marketing" in out
    assert "design-extras" in out
    assert "enabled" in out
    assert "disabled" in out


def test_packs_enable_persists_intent_even_on_install_failure(env):
    calls = _fake_packs(
        env.monkeypatch,
        install_result=packs_mod.PackOpResult(
            ok=False,
            missing=list(MARKETING.skills),
            message="npx skills add failed.",
        ),
    )
    _stub_codex(env.monkeypatch, present=False)
    r = CliRunner().invoke(main, ["packs", "enable", "marketing"])
    assert r.exit_code == 1
    # Intent persisted BEFORE the (failed) install, so a later update can repair.
    assert any(c[1] == "marketing" and c[2] is True for c in calls.set_enabled)
    assert len(calls.install_pack) == 1
    assert "[FAIL]" in env.buf.getvalue()


def test_packs_disable_disables_before_remove_on_failure(env):
    calls = _fake_packs(
        env.monkeypatch,
        enabled_before=["marketing"],
        remove_result=packs_mod.PackOpResult(
            ok=False, message="npx skills remove failed."
        ),
    )
    r = CliRunner().invoke(main, ["packs", "disable", "marketing"])
    assert r.exit_code == 1
    assert len(calls.remove_pack) == 1
    # Disable intent is durable BEFORE removal, and wins even when removal failed.
    assert any(c[1] == "marketing" and c[2] is False for c in calls.set_enabled)
    kinds = [c[0] for c in calls.order]
    assert kinds.index("set_enabled") < kinds.index("remove_pack")
    out = env.buf.getvalue()
    assert "[FAIL]" in out
    # On failure the user is told skills may linger and how to retry.
    assert "Enable and disable again to retry removal" in out


def test_packs_update_no_enabled_says_so_and_exits_zero(env):
    calls = _fake_packs(env.monkeypatch, enabled_before=[])
    r = CliRunner().invoke(main, ["packs", "update"])
    assert r.exit_code == 0
    assert calls.update_packs == []
    assert "No skill packs are enabled" in env.buf.getvalue()


def test_packs_update_unknown_name_lists_valid(env):
    _fake_packs(env.monkeypatch, enabled_before=["marketing"])
    r = CliRunner().invoke(main, ["packs", "update", "bogus"])
    assert r.exit_code == 1
    out = env.buf.getvalue()
    assert "Unknown skill pack" in out
    assert "marketing" in out


# --------------------------------------------------------------------------- #
# uninstall integration
# --------------------------------------------------------------------------- #

def test_uninstall_removes_packs_and_deletes_state(env):
    calls = _fake_packs(env.monkeypatch, enabled_before=["marketing"])
    # Keep the full uninstall hermetic: no real Codex / Chrome MCP side effects.
    import jacked.codex.installer as cdx

    env.monkeypatch.setattr(
        cdx, "uninstall_codex", lambda *a, **k: {"removed": [], "skipped": []}
    )
    env.monkeypatch.setattr(cli, "_remove_chrome_devtools_mcp", lambda *a, **k: False)

    state_file = env.home / ".claude" / packs_mod.STATE_PATH_NAME
    state_file.write_text('{"version":1,"enabled":{"marketing":{}}}', encoding="utf-8")

    r = CliRunner().invoke(main, ["uninstall", "--yes"])
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    assert len(calls.remove_pack) == 1
    assert calls.remove_pack[0].pack.name == "marketing"
    assert not state_file.exists()
    assert "Pack 'marketing': removed" in env.buf.getvalue()


def _stub_uninstall_side_effects(env):
    """Neutralize the Codex + Chrome MCP side effects the full uninstall hits."""
    import jacked.codex.installer as cdx

    env.monkeypatch.setattr(
        cdx, "uninstall_codex", lambda *a, **k: {"removed": [], "skipped": []}
    )
    env.monkeypatch.setattr(cli, "_remove_chrome_devtools_mcp", lambda *a, **k: False)


def _write_pack_state(env, *enabled_names):
    """Write a pack state file enabling the given pack names."""
    entries = ",".join(f'"{n}":{{}}' for n in enabled_names)
    state_file = env.home / ".claude" / packs_mod.STATE_PATH_NAME
    state_file.write_text(f'{{"version":1,"enabled":{{{entries}}}}}', encoding="utf-8")
    return state_file


def test_uninstall_npx_missing_warns_and_deletes_state(env):
    calls = _fake_packs(env.monkeypatch, enabled_before=["marketing"], npx=None)
    _stub_uninstall_side_effects(env)
    state_file = _write_pack_state(env, "marketing")

    r = CliRunner().invoke(main, ["uninstall", "--yes"])
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    # No npx -> we can't drive removal, but state is still cleared.
    assert calls.remove_pack == []
    assert "npx not found" in env.buf.getvalue()
    assert not state_file.exists()


# --------------------------------------------------------------------------- #
# install --packs validation happens before ANY install work
# --------------------------------------------------------------------------- #

def test_install_unknown_pack_exits_before_run_install(env):
    _fake_packs(env.monkeypatch)
    ran = []
    env.monkeypatch.setattr(cli, "_run_install", lambda **k: ran.append(True))
    r = CliRunner().invoke(main, ["install", "--no-codex", "--packs", "bogus"])
    assert r.exit_code == 1
    # The sentinel proves validation short-circuited before the install ran.
    assert ran == []
    out = env.buf.getvalue()
    assert "Unknown skill pack" in out
    assert "marketing" in out and "design-extras" in out


def test_install_duplicate_packs_single_install(env):
    calls = _fake_packs(env.monkeypatch)
    _stub_codex(env.monkeypatch, present=False)
    r = CliRunner().invoke(
        main, ["install", "--no-codex", "--packs", "marketing,marketing"]
    )
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    # Duplicate names collapse to one enable + one install.
    assert len(calls.install_pack) == 1
    assert [c for c in calls.set_enabled if c[1] == "marketing" and c[2] is True]


# --------------------------------------------------------------------------- #
# install --packs: skill-packs phase failures are contained
# --------------------------------------------------------------------------- #

def test_install_packs_phase_exception_is_contained(env):
    _fake_packs(env.monkeypatch)

    def boom(*a, **k):
        raise OSError("disk gone")

    env.monkeypatch.setattr(packs_mod, "set_enabled", boom)
    r = CliRunner().invoke(main, ["install", "--no-codex", "--packs", "marketing"])
    # An in-phase blowup never fails the overall install.
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    assert "Skill packs phase failed" in env.buf.getvalue()


# --------------------------------------------------------------------------- #
# per-pack attribution on the batched update path
# --------------------------------------------------------------------------- #

def _partial_update(healthy="marketing", broken="design-extras"):
    """A batched-update result: one pack healthy, one failed, aggregate ok=False."""
    return SimpleNamespace(
        ok=False,  # aggregate reflects the batch, NOT the healthy sibling
        message="batch partial failure",
        per_pack={
            healthy: packs_mod.PackOpResult(
                ok=True, installed=list(MARKETING.skills), message="marketing ok"
            ),
            broken: packs_mod.PackOpResult(
                ok=False, missing=list(DESIGN.skills), message="design-extras failed"
            ),
        },
    )


def test_install_update_per_pack_console_attribution(env):
    _fake_packs(
        env.monkeypatch,
        enabled_before=["marketing", "design-extras"],
        update_result=_partial_update(),
    )
    r = CliRunner().invoke(main, ["install", "--no-codex"])
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    out = env.buf.getvalue()
    # The healthy pack must NOT inherit the sibling's failure.
    assert "[OK] Pack 'marketing'" in out
    assert "[FAIL]" in out and "design-extras failed" in out


def test_install_update_per_pack_json_mirrors_truth(env):
    _fake_packs(
        env.monkeypatch,
        enabled_before=["marketing", "design-extras"],
        update_result=_partial_update(),
    )
    r = CliRunner().invoke(main, ["install", "--no-codex", "--json"])
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    line = [ln for ln in r.output.splitlines() if ln.strip()][-1]
    rec = json.loads(line)
    assert rec["packs"]["marketing"]["ok"] is True
    assert rec["packs"]["design-extras"]["ok"] is False
    assert rec["packs"]["design-extras"]["missing"] == list(DESIGN.skills)


# --------------------------------------------------------------------------- #
# disable ordering: durable intent before removal
# --------------------------------------------------------------------------- #

def test_packs_disable_sets_disabled_before_remove(env):
    calls = _fake_packs(env.monkeypatch, enabled_before=["marketing"])
    r = CliRunner().invoke(main, ["packs", "disable", "marketing"])
    assert r.exit_code == 0
    kinds = [c[0] for c in calls.order]
    assert kinds.index("set_enabled") < kinds.index("remove_pack")
    first_set = next(c for c in calls.order if c[0] == "set_enabled")
    assert first_set == ("set_enabled", "marketing", False)


# --------------------------------------------------------------------------- #
# deregistered (enabled-but-unknown) packs are surfaced, never silently skipped
# --------------------------------------------------------------------------- #

_DEREG_WARN = "Pack 'ghost' is enabled but unknown"


def test_install_phase_warns_on_deregistered_pack(env):
    _fake_packs(env.monkeypatch, enabled_before=["ghost"])
    r = CliRunner().invoke(main, ["install", "--no-codex"])
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    assert _DEREG_WARN in env.buf.getvalue()


def test_packs_list_warns_on_deregistered_pack(env):
    _fake_packs(env.monkeypatch, enabled_before=["ghost"])
    r = CliRunner().invoke(main, ["packs", "list"])
    assert r.exit_code == 0
    assert _DEREG_WARN in env.buf.getvalue()


def test_uninstall_warns_on_deregistered_pack(env):
    _fake_packs(env.monkeypatch, enabled_before=["ghost"])
    _stub_uninstall_side_effects(env)
    state_file = _write_pack_state(env, "ghost")

    r = CliRunner().invoke(main, ["uninstall", "--yes"])
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    assert _DEREG_WARN in env.buf.getvalue()
    assert not state_file.exists()


def test_packs_disable_deregistered_clears_state_with_warning(env):
    calls = _fake_packs(env.monkeypatch, enabled_before=["ghost"])
    r = CliRunner().invoke(main, ["packs", "disable", "ghost"])
    assert r.exit_code == 0
    # We can't enumerate an unknown pack's skills, so nothing is removed...
    assert calls.remove_pack == []
    # ...but the stuck-enabled state entry is turned off, loudly.
    assert any(c[1] == "ghost" and c[2] is False for c in calls.set_enabled)
    assert "unknown to this jacked version but was enabled" in env.buf.getvalue()


# --------------------------------------------------------------------------- #
# trust / provenance line before pulling instructions the agents will run
# --------------------------------------------------------------------------- #

def test_packs_enable_prints_trust_line(env):
    _fake_packs(env.monkeypatch)
    _stub_codex(env.monkeypatch, present=False)
    r = CliRunner().invoke(main, ["packs", "enable", "marketing"])
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    out = env.buf.getvalue()
    assert "review the source at" in out
    assert MARKETING.source in out
    assert MARKETING.homepage in out
    assert "—" not in out  # no em-dashes in user-facing copy


def test_install_packs_prints_trust_line_per_new_pack(env):
    _fake_packs(env.monkeypatch)
    _stub_codex(env.monkeypatch, present=False)
    r = CliRunner().invoke(main, ["install", "--no-codex", "--packs", "marketing"])
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    out = env.buf.getvalue()
    assert "review the source at" in out
    assert MARKETING.source in out


# --------------------------------------------------------------------------- #
# install_pack skipped (pre-existing user-owned skill dir) is loud + recorded
# --------------------------------------------------------------------------- #

def _skipped_install(_pack):
    return packs_mod.PackOpResult(
        ok=True,
        installed=list(MARKETING.skills),
        skipped=["pricing"],
        message="Installed; skipped 1 dir you already own.",
    )


def test_install_packs_skipped_printed_loudly(env):
    _fake_packs(env.monkeypatch, install_result=_skipped_install)
    _stub_codex(env.monkeypatch, present=False)
    r = CliRunner().invoke(main, ["install", "--no-codex", "--packs", "marketing"])
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    out = env.buf.getvalue()
    assert "pricing" in out
    assert "untouched" in out


def test_install_packs_skipped_recorded_in_json(env):
    _fake_packs(env.monkeypatch, install_result=_skipped_install)
    _stub_codex(env.monkeypatch, present=False)
    r = CliRunner().invoke(
        main, ["install", "--no-codex", "--json", "--packs", "marketing"]
    )
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    line = [ln for ln in r.output.splitlines() if ln.strip()][-1]
    rec = json.loads(line)
    assert rec["packs"]["marketing"]["skipped"] == ["pricing"]

def test_pack_failure_message_with_rich_markup_does_not_crash(env):
    """npm error tails routinely contain bracketed tokens; a hostile or merely
    unlucky message must neither raise rich.errors.MarkupError nor render live
    [link] markup. Regression for the Rich-injection finding."""
    hostile = "npx skills exited 1. npm ERR! [/bad] [link=http://evil.example]click[/link]"
    _fake_packs(
        env.monkeypatch,
        install_result=packs_mod.PackOpResult(ok=False, message=hostile),
    )
    _stub_codex(env.monkeypatch, present=False)
    r = CliRunner().invoke(main, ["packs", "enable", "marketing"])
    assert r.exit_code == 1
    out = env.buf.getvalue()
    assert "npm ERR!" in out
    # the [link=...] token must appear as inert text, not be swallowed as markup
    assert "evil.example" in out
