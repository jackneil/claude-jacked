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

# A default-on pack: installed by a plain `jacked install` unless the user has
# durably opted out (default=True is the crux of the state-model change these
# tests cover). The base MARKETING/DESIGN packs stay default=False so the
# existing explicit `--packs`/enable tests keep their meaning and a plain
# install in the base registry installs nothing by default.
DEFAULT_ON = packs_mod.Pack(
    name="core-extras",
    display_name="Core Extras",
    description="Default-on curated skills that install unless opted out.",
    source="jackneil/coreextras",
    homepage="https://github.com/jackneil/coreextras",
    skills=("triage", "summarize"),
    default=True,
)


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
    disabled_before=None,
    installed_before=None,
    foreign_before=None,
    npx="/usr/bin/npx",
    install_result=None,
    update_result=None,
    remove_result=None,
    status=None,
):
    """Replace every jacked.packs entrypoint with a recording fake.

    Models both halves of the v2 state model the CLI now depends on:

    * explicit enable/disable decisions (``state_map``: a name maps to
      ``"enabled"``/``"disabled"``, or is absent when no explicit decision was
      recorded, in which case the registry ``default`` flag decides). Seed with
      ``enabled_before`` / ``disabled_before``.
    * disk-installed skills (``installed_disk``). This is what the install-vs-
      update discriminator in ``_run_packs_phase`` reads via ``pack_status``: a
      pack whose skills are all present routes to a batched update, one that
      isn't routes to a fresh install. ``installed_before`` seeds it; it
      DEFAULTS to the skills of every ``enabled_before`` pack (a pack enabled
      before was presumably installed), so already-enabled packs route to
      update without extra wiring.

    Returns a namespace of call records. ``*_result`` may be a PackOpResult or a
    callable(pack)->PackOpResult; ``status`` similarly for pack_status.
    """
    calls = SimpleNamespace(
        set_enabled=[], install_pack=[], update_packs=[], remove_pack=[],
        pack_status=[], order=[],
    )

    # Explicit decisions only. Absence == "no explicit decision" (default rules).
    state_map: dict[str, str] = {}
    for _n in (enabled_before or []):
        state_map[_n] = "enabled"
    for _n in (disabled_before or []):
        state_map[_n] = "disabled"

    # Disk-installed skill set. Defaults to the skills of every enabled_before
    # pack so a previously-enabled pack reads as fully installed on disk.
    if installed_before is not None:
        installed_disk: set[str] = set(installed_before)
    else:
        installed_disk = set()
        for _n in (enabled_before or []):
            _p = registry.get(_n)
            if _p is not None:
                installed_disk.update(_p.skills)

    # Skills present on disk but NOT ours (a same-named dir from another source
    # or a user's own skill). These read installed=True but source_ok=False, so
    # a pack fully shadowed by them must route to install (where the real
    # collision guard surfaces the shadow), never to a silent "up to date".
    foreign_disk: set[str] = set(foreign_before or [])

    def load_registry(data_root):
        return dict(registry)

    def set_enabled(home, name, enabled):
        calls.set_enabled.append((str(home), name, enabled))
        calls.order.append(("set_enabled", name, enabled))
        # v2: both directions write a durable state entry (disable does NOT drop
        # the entry — that is what lets a default-on pack be durably opted out).
        state_map[name] = "enabled" if enabled else "disabled"

    def pack_state(home, name):
        return state_map.get(name)

    def enabled_pack_names(home):
        # EXPLICITLY-enabled only (default-on-but-untoggled packs excluded).
        return sorted(n for n, s in state_map.items() if s == "enabled")

    def is_effectively_enabled(pack, home):
        s = state_map.get(pack.name)
        if s is not None:
            return s == "enabled"
        return pack.default

    def effective_enabled_pack_names(home, reg):
        return sorted(n for n, p in reg.items() if is_effectively_enabled(p, home))

    def find_npx():
        return npx

    def install_pack(pack, home, *, include_codex, timeout=600):
        calls.install_pack.append(SimpleNamespace(pack=pack, include_codex=include_codex))
        calls.order.append(("install_pack", pack.name))
        if install_result is not None:
            res = install_result(pack) if callable(install_result) else install_result
        else:
            res = packs_mod.PackOpResult(
                ok=True,
                installed=list(pack.skills),
                message=f"Installed {len(pack.skills)} skill(s) for pack '{pack.display_name}'.",
            )
        # Model disk: a successful install lands its reported skills on disk (a
        # failure result carries installed=[], so nothing lands).
        installed_disk.update(getattr(res, "installed", None) or [])
        return res

    def update_packs(pks, home, *, include_codex, timeout=600):
        calls.update_packs.append(
            SimpleNamespace(packs=list(pks), include_codex=include_codex)
        )
        calls.order.append(("update_packs", [p.name for p in pks]))
        if update_result is not None:
            return update_result
        # Mirror the real contract: aggregate fields PLUS a per_pack dict of
        # per-pack PackOpResults keyed by pack name.
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
            res = remove_result(pack) if callable(remove_result) else remove_result
        else:
            res = packs_mod.PackOpResult(
                ok=True,
                removed=list(pack.skills),
                message=f"Removed {len(pack.skills)} skill(s) for pack '{pack.display_name}'.",
            )
        # Model disk: removal takes this pack's skills off disk.
        installed_disk.difference_update(pack.skills)
        return res

    def pack_status(pack, home):
        calls.pack_status.append(pack.name)
        if status is not None:
            return status(pack) if callable(status) else status
        skills = []
        for s in pack.skills:
            if s in installed_disk:
                skills.append({"name": s, "installed": True, "source_ok": True})
            elif s in foreign_disk:
                # on disk, but not ours (foreign source / user's own dir)
                skills.append({"name": s, "installed": True, "source_ok": False})
            else:
                skills.append({"name": s, "installed": False, "source_ok": None})
        return {
            "name": pack.name,
            "installed_count": sum(1 for s in skills if s["installed"]),
            "total": len(pack.skills),
            "skills": skills,
        }

    for fn in (
        load_registry,
        set_enabled,
        pack_state,
        enabled_pack_names,
        is_effectively_enabled,
        effective_enabled_pack_names,
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
    # marketing is explicitly enabled; design-extras is default=False and never
    # toggled. Both packs render, each with its correct v2 status label.
    _fake_packs(env.monkeypatch, enabled_before=["marketing"])
    r = CliRunner().invoke(main, ["packs", "list"])
    assert r.exit_code == 0
    out = env.buf.getvalue()
    assert "marketing" in out
    assert "design-extras" in out
    lines = out.splitlines()
    mk_line = next(ln for ln in lines if "marketing" in ln)
    dx_line = next(ln for ln in lines if "design-extras" in ln)
    # Explicitly-enabled -> effectively on, WITHOUT the "(default)" suffix
    # (that suffix marks a pack that's on purely because default=True).
    assert "on" in mk_line
    assert "(default)" not in mk_line
    # default=False and never enabled -> off, flagged as opt-in (not a disable).
    assert "off (opt-in)" in dx_line


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


# --------------------------------------------------------------------------- #
# default-on packs: a plain `install` installs default:true packs, opt-out is
# durable, and --no-packs skips the phase entirely
# --------------------------------------------------------------------------- #

def test_install_default_on_installs_without_flag(env):
    # A registry with one default-on and one default-off pack; a bare install
    # (no --packs) installs the default-on one and leaves the default-off alone.
    registry = {"core-extras": DEFAULT_ON, "marketing": MARKETING}
    calls = _fake_packs(env.monkeypatch, registry=registry)
    r = CliRunner().invoke(main, ["install", "--no-codex"])
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    installed_names = [c.pack.name for c in calls.install_pack]
    assert installed_names == ["core-extras"]  # default-on installs by itself
    assert "marketing" not in installed_names  # default-off is untouched
    assert calls.update_packs == []
    out = env.buf.getvalue()
    # The trust line prints before pulling a fresh pack's instructions.
    assert "review the source at" in out
    assert DEFAULT_ON.source in out


def test_install_no_packs_flag_skips(env):
    # --no-packs short-circuits the whole phase: a default-on pack that would
    # otherwise install this run is skipped, and nothing is installed/updated.
    registry = {"core-extras": DEFAULT_ON}
    calls = _fake_packs(env.monkeypatch, registry=registry)
    r = CliRunner().invoke(main, ["install", "--no-codex", "--no-packs"])
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    assert calls.install_pack == []
    assert calls.update_packs == []
    assert "skipped (--no-packs)" in env.buf.getvalue()


def test_install_no_packs_with_packs_is_error(env):
    # Contradictory flags fail fast, before ANY state mutation or install work.
    calls = _fake_packs(env.monkeypatch)
    r = CliRunner().invoke(
        main, ["install", "--no-codex", "--no-packs", "--packs", "marketing"]
    )
    assert r.exit_code == 1
    assert calls.set_enabled == []
    assert calls.install_pack == []
    assert calls.update_packs == []
    assert "--no-packs and --packs are contradictory" in env.buf.getvalue()


def test_disabled_default_pack_not_reinstalled_on_install(env):
    # The whole point of the durable-disable state: a default-on pack the user
    # opted out of must NOT silently reinstall on the next `jacked install`.
    registry = {"core-extras": DEFAULT_ON}
    calls = _fake_packs(
        env.monkeypatch, registry=registry, disabled_before=["core-extras"]
    )
    r = CliRunner().invoke(main, ["install", "--no-codex"])
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    assert calls.install_pack == []  # explicit disable beats default=True
    assert calls.update_packs == []


def test_uninstall_removes_default_on_pack(env):
    # Uninstall operates on the EFFECTIVE set: a default-on pack that's on with
    # no explicit state entry is still removed (not just explicit-enabled packs).
    registry = {"core-extras": DEFAULT_ON}
    calls = _fake_packs(env.monkeypatch, registry=registry)
    _stub_uninstall_side_effects(env)
    r = CliRunner().invoke(main, ["uninstall", "--yes"])
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    assert [c.pack.name for c in calls.remove_pack] == ["core-extras"]
    assert "Pack 'core-extras': removed" in env.buf.getvalue()


def test_install_foreign_shadowed_pack_routes_to_install_not_silent_update(env):
    # A default-on pack whose skills are all present on disk but NOT ours (a
    # same-named dir from another source / the user's own) must route to
    # install, where the collision guard surfaces the shadow -- never to a
    # silent batched update that reports "up to date" while installing nothing.
    registry = {"core-extras": DEFAULT_ON}
    calls = _fake_packs(
        env.monkeypatch, registry=registry,
        foreign_before=list(DEFAULT_ON.skills),
    )
    r = CliRunner().invoke(main, ["install", "--no-codex"])
    assert r.exit_code == 0, r.output + env.buf.getvalue()
    # Routed to install (collision guard runs there), not the silent update path.
    assert [c.pack.name for c in calls.install_pack] == ["core-extras"]
    assert calls.update_packs == []
