"""Unit tests for the /freeze boundary in the security gatekeeper hook.

Covers the freeze upgrade: fail-closed on corruption, multi-path includes,
sub-path excludes, per-project scoping, legacy single-path fallback, and the
"active since" surfacing. Tests the pure functions directly (no subprocess).
"""

import json
import sys
from pathlib import Path

# Add the gatekeeper module to path so we can import it directly
GATEKEEPER_DIR = (
    Path(__file__).resolve().parent.parent.parent / "jacked" / "data" / "hooks"
)
sys.path.insert(0, str(GATEKEEPER_DIR))

import security_gatekeeper as gk  # noqa: E402


def _write_json(path: Path, freezes) -> None:
    path.write_text(json.dumps({"freezes": freezes}), encoding="utf-8")


def _norm(p) -> str:
    return gk._freeze_norm(str(p))


# ---------------------------------------------------------------------------
# _load_freeze_entries — corruption / fail-closed
# ---------------------------------------------------------------------------


class TestLoadFreezeEntries:
    def test_no_files_no_freeze(self, tmp_path):
        entries, corrupt = gk._load_freeze_entries(
            tmp_path / "missing.json", tmp_path / "missing.txt"
        )
        assert entries is None
        assert corrupt is False

    def test_empty_json_is_not_corrupt(self, tmp_path):
        jp = tmp_path / "freeze.json"
        jp.write_text("   \n", encoding="utf-8")
        entries, corrupt = gk._load_freeze_entries(jp, tmp_path / "x.txt")
        assert entries is None
        assert corrupt is False  # empty == no freeze, never bricks edits

    def test_malformed_json_is_corrupt(self, tmp_path):
        jp = tmp_path / "freeze.json"
        jp.write_text("{not json", encoding="utf-8")
        entries, corrupt = gk._load_freeze_entries(jp, tmp_path / "x.txt")
        assert entries is None
        assert corrupt is True  # fail closed

    def test_wrong_shape_is_corrupt(self, tmp_path):
        jp = tmp_path / "freeze.json"
        jp.write_text(json.dumps({"freezes": "nope"}), encoding="utf-8")
        entries, corrupt = gk._load_freeze_entries(jp, tmp_path / "x.txt")
        assert corrupt is True

    def test_entry_missing_include_is_corrupt(self, tmp_path):
        jp = tmp_path / "freeze.json"
        _write_json(jp, [{"project": "/x", "include": []}])
        entries, corrupt = gk._load_freeze_entries(jp, tmp_path / "x.txt")
        assert corrupt is True

    def test_valid_single_include(self, tmp_path):
        jp = tmp_path / "freeze.json"
        _write_json(jp, [{"project": "/repo", "include": ["/repo/src"]}])
        entries, corrupt = gk._load_freeze_entries(jp, tmp_path / "x.txt")
        assert corrupt is False
        assert entries == [
            {
                "project": "/repo",
                "include": ["/repo/src"],
                "exclude": [],
                "since": None,
            }
        ]

    def test_string_include_coerced_to_list(self, tmp_path):
        jp = tmp_path / "freeze.json"
        _write_json(jp, [{"project": "/repo", "include": "/repo/src"}])
        entries, _ = gk._load_freeze_entries(jp, tmp_path / "x.txt")
        assert entries[0]["include"] == ["/repo/src"]

    def test_legacy_txt_fallback(self, tmp_path):
        tp = tmp_path / "freeze.txt"
        tp.write_text("/legacy/dir\n", encoding="utf-8")
        entries, corrupt = gk._load_freeze_entries(tmp_path / "missing.json", tp)
        assert corrupt is False
        assert entries == [
            {"project": None, "include": ["/legacy/dir"], "exclude": [], "since": None}
        ]

    def test_empty_legacy_txt_is_not_corrupt(self, tmp_path):
        tp = tmp_path / "freeze.txt"
        tp.write_text("", encoding="utf-8")
        entries, corrupt = gk._load_freeze_entries(tmp_path / "missing.json", tp)
        assert entries is None
        assert corrupt is False

    def test_json_takes_precedence_over_legacy(self, tmp_path):
        jp = tmp_path / "freeze.json"
        tp = tmp_path / "freeze.txt"
        _write_json(jp, [{"project": "/repo", "include": ["/repo/src"]}])
        tp.write_text("/legacy/dir\n", encoding="utf-8")
        entries, _ = gk._load_freeze_entries(jp, tp)
        assert entries[0]["include"] == ["/repo/src"]  # JSON wins


# ---------------------------------------------------------------------------
# _freeze_entries_for_project — per-project scoping
# ---------------------------------------------------------------------------


class TestProjectScoping:
    def test_project_entry_matches_only_its_repo(self, tmp_path):
        repo_a = tmp_path / "repoA"
        repo_b = tmp_path / "repoB"
        repo_a.mkdir()
        repo_b.mkdir()
        entries = [{"project": str(repo_a), "include": [str(repo_a / "src")], "exclude": [], "since": None}]
        assert gk._freeze_entries_for_project(entries, str(repo_a)) == entries
        # Freeze in repo A must NOT govern repo B.
        assert gk._freeze_entries_for_project(entries, str(repo_b)) == []

    def test_global_entry_applies_everywhere(self, tmp_path):
        entries = [{"project": None, "include": ["/any"], "exclude": [], "since": None}]
        assert gk._freeze_entries_for_project(entries, "/repoA") == entries
        assert gk._freeze_entries_for_project(entries, "/repoB") == entries

    def test_star_project_is_global(self):
        entries = [{"project": "*", "include": ["/any"], "exclude": [], "since": None}]
        assert gk._freeze_entries_for_project(entries, "/whatever") == entries


# ---------------------------------------------------------------------------
# _freeze_allows — include/exclude semantics
# ---------------------------------------------------------------------------


class TestFreezeAllows:
    def test_inside_include_allowed(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        e = [{"include": [str(src)], "exclude": []}]
        assert gk._freeze_allows(str(src / "a.py"), e) is True

    def test_outside_include_denied(self, tmp_path):
        src = tmp_path / "src"
        other = tmp_path / "other"
        src.mkdir()
        other.mkdir()
        e = [{"include": [str(src)], "exclude": []}]
        assert gk._freeze_allows(str(other / "a.py"), e) is False

    def test_excluded_subpath_denied(self, tmp_path):
        src = tmp_path / "src"
        secrets = src / "secrets"
        secrets.mkdir(parents=True)
        e = [{"include": [str(src)], "exclude": [str(secrets)]}]
        assert gk._freeze_allows(str(src / "a.py"), e) is True
        assert gk._freeze_allows(str(secrets / "k.py"), e) is False

    def test_multiple_includes_union(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        e = [{"include": [str(a), str(b)], "exclude": []}]
        assert gk._freeze_allows(str(a / "x.py"), e) is True
        assert gk._freeze_allows(str(b / "x.py"), e) is True
        assert gk._freeze_allows(str(tmp_path / "c.py"), e) is False

    def test_include_dir_itself_allowed(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        e = [{"include": [str(src)], "exclude": []}]
        assert gk._freeze_allows(str(src), e) is True

    def test_sibling_prefix_not_matched(self, tmp_path):
        # /repo/src must not match /repo/src-extra (prefix-without-slash bug guard)
        src = tmp_path / "src"
        sibling = tmp_path / "src-extra"
        src.mkdir()
        sibling.mkdir()
        e = [{"include": [str(src)], "exclude": []}]
        assert gk._freeze_allows(str(sibling / "a.py"), e) is False

    def test_symlink_escaping_frozen_dir_is_caught(self, tmp_path):
        # A symlink inside the frozen dir pointing outside must be DENIED: the
        # boundary applies to the resolved target, not the link path.
        src = tmp_path / "src"
        outside = tmp_path / "outside"
        src.mkdir()
        outside.mkdir()
        real = outside / "real.py"
        real.write_text("x = 1")
        link = src / "link.py"
        try:
            link.symlink_to(real)
        except (OSError, NotImplementedError):
            import pytest

            pytest.skip("symlinks unsupported on this platform")
        e = [{"include": [str(src)], "exclude": []}]
        # Link lives under src/, but resolves to outside/ → not allowed.
        assert gk._freeze_allows(str(link), e) is False


# ---------------------------------------------------------------------------
# evaluate_freeze_boundary — end to end
# ---------------------------------------------------------------------------


class TestEvaluateFreezeBoundary:
    def test_read_tool_never_restricted(self, tmp_path):
        jp = tmp_path / "freeze.json"
        _write_json(jp, [{"project": None, "include": ["/nowhere"]}])
        assert (
            gk.evaluate_freeze_boundary(
                "Read", "/anything/else.py", "/repo", json_path=jp, txt_path=tmp_path / "x.txt"
            )
            is None
        )

    def test_no_freeze_allows(self, tmp_path):
        assert (
            gk.evaluate_freeze_boundary(
                "Write", "/x.py", "/repo",
                json_path=tmp_path / "missing.json", txt_path=tmp_path / "missing.txt",
            )
            is None
        )

    def test_corrupt_fails_closed(self, tmp_path):
        jp = tmp_path / "freeze.json"
        jp.write_text("{broken", encoding="utf-8")
        decision = gk.evaluate_freeze_boundary(
            "Edit", "/x.py", "/repo", json_path=jp, txt_path=tmp_path / "x.txt"
        )
        assert decision is not None
        user_msg, log_reason = decision
        assert "corrupt" in user_msg.lower()
        assert "/unfreeze" in user_msg
        assert "fail-closed" in log_reason

    def test_inside_scope_allows(self, tmp_path):
        repo = tmp_path / "repo"
        src = repo / "src"
        src.mkdir(parents=True)
        jp = tmp_path / "freeze.json"
        _write_json(jp, [{"project": str(repo), "include": [str(src)]}])
        assert (
            gk.evaluate_freeze_boundary(
                "Edit", str(src / "a.py"), str(repo), json_path=jp, txt_path=tmp_path / "x.txt"
            )
            is None
        )

    def test_outside_scope_denies(self, tmp_path):
        repo = tmp_path / "repo"
        src = repo / "src"
        other = repo / "other"
        src.mkdir(parents=True)
        other.mkdir(parents=True)
        jp = tmp_path / "freeze.json"
        _write_json(jp, [{"project": str(repo), "include": [str(src)]}])
        decision = gk.evaluate_freeze_boundary(
            "Write", str(other / "a.py"), str(repo), json_path=jp, txt_path=tmp_path / "x.txt"
        )
        assert decision is not None
        assert "restricted to" in decision[0]

    def test_other_project_not_governed(self, tmp_path):
        repo_a = tmp_path / "repoA"
        repo_b = tmp_path / "repoB"
        (repo_a / "src").mkdir(parents=True)
        repo_b.mkdir()
        jp = tmp_path / "freeze.json"
        _write_json(jp, [{"project": str(repo_a), "include": [str(repo_a / "src")]}])
        # Editing in repo B while a freeze is set for repo A → allowed.
        assert (
            gk.evaluate_freeze_boundary(
                "Edit", str(repo_b / "a.py"), str(repo_b), json_path=jp, txt_path=tmp_path / "x.txt"
            )
            is None
        )

    def test_active_since_surfaced(self, tmp_path):
        repo = tmp_path / "repo"
        src = repo / "src"
        src.mkdir(parents=True)
        jp = tmp_path / "freeze.json"
        _write_json(
            jp,
            [{"project": str(repo), "include": [str(src)], "since": "2026-06-26T14:30:00Z"}],
        )
        decision = gk.evaluate_freeze_boundary(
            "Edit", str(repo / "out.py"), str(repo), json_path=jp, txt_path=tmp_path / "x.txt"
        )
        assert decision is not None
        assert "active since 2026-06-26T14:30:00Z" in decision[0]

    def test_excluded_subpath_surfaced_and_denied(self, tmp_path):
        repo = tmp_path / "repo"
        src = repo / "src"
        secrets = src / "secrets"
        secrets.mkdir(parents=True)
        jp = tmp_path / "freeze.json"
        _write_json(
            jp,
            [{"project": str(repo), "include": [str(src)], "exclude": [str(secrets)]}],
        )
        decision = gk.evaluate_freeze_boundary(
            "Edit", str(secrets / "k.py"), str(repo), json_path=jp, txt_path=tmp_path / "x.txt"
        )
        assert decision is not None
        assert "excluded:" in decision[0]

    def test_legacy_txt_global_denies_outside(self, tmp_path):
        frozen = tmp_path / "frozen"
        outside = tmp_path / "outside"
        frozen.mkdir()
        outside.mkdir()
        tp = tmp_path / "freeze.txt"
        tp.write_text(str(frozen), encoding="utf-8")
        decision = gk.evaluate_freeze_boundary(
            "Edit", str(outside / "a.py"), str(tmp_path),
            json_path=tmp_path / "missing.json", txt_path=tp,
        )
        assert decision is not None  # legacy global freeze still enforced
        assert (
            gk.evaluate_freeze_boundary(
                "Edit", str(frozen / "a.py"), str(tmp_path),
                json_path=tmp_path / "missing.json", txt_path=tp,
            )
            is None
        )
