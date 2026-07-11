# tests/unit/integrations/test_vet_script.py
"""Offline tests for the Agent-Reach vetting pipeline (scripts/).

Exercises ref->SHA resolution against a local fixture repo, skill hashing, pin
JSON emission + schema round-trip, the diff-vs-current-pin path, and the
channel-table build with stubbed network resolvers. Nothing here reaches the
network: git runs against a tmp_path repo, and npm/PyPI lookups are monkeypatched.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

# scripts/ is not an installed package; make it importable.
SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import _vet_helpers as vh  # noqa: E402
import vet_agent_reach as vas  # noqa: E402


# ── Fixture repo helpers ─────────────────────────────────────────────────────

def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True, capture_output=True)
    _git(root, "config", "user.email", "vet@test")
    _git(root, "config", "user.name", "Vet Test")
    return root


def _write_skill(root: Path, refs=("dev", "social")) -> None:
    skill = root / "agent_reach" / "skill"
    (skill / "references").mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text("# skill v1\n", encoding="utf-8")
    (skill / "SKILL_en.md").write_text("# skill en v1\n", encoding="utf-8")
    for name in refs:
        (skill / "references" / f"{name}.md").write_text(f"# {name}\n", encoding="utf-8")


def _write_pyproject(root: Path, deps: list[str], version: str = "1.5.0") -> None:
    body = "[project]\nname = \"agent-reach\"\nversion = \"%s\"\ndependencies = [\n%s\n]\n" % (
        version,
        "\n".join(f'    "{d}",' for d in deps),
    )
    (root / "pyproject.toml").write_text(body, encoding="utf-8")


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=vet@test", "-c", "user.name=Vet Test", "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


# ── ref -> SHA ───────────────────────────────────────────────────────────────

def test_resolve_ref_to_sha_from_local_repo(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _write_skill(repo)
    _write_pyproject(repo, ["requests>=2.28"])
    head = _commit(repo, "init")

    resolved = vh.resolve_ref_to_sha(str(repo), "main")
    assert vh.SHA_RE.match(resolved)
    assert resolved == head


def test_resolve_ref_passes_through_full_sha():
    sha = "a" * 40
    assert vh.resolve_ref_to_sha("https://example.invalid", sha.upper()) == sha


def test_resolve_ref_unknown_raises(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _write_skill(repo)
    _write_pyproject(repo, ["requests>=2.28"])
    _commit(repo, "init")
    with pytest.raises(vh.VetError):
        vh.resolve_ref_to_sha(str(repo), "no-such-ref")


# ── skill hashing ────────────────────────────────────────────────────────────

def test_hash_skill_payload_keys_and_values(tmp_path):
    repo = tmp_path / "repo"
    _write_skill(repo, refs=("dev", "social", "web"))
    hashes = vh.hash_skill_payload(repo)

    assert set(hashes) == {
        "SKILL.md",
        "SKILL_en.md",
        "references/dev.md",
        "references/social.md",
        "references/web.md",
    }
    for value in hashes.values():
        assert vh.SHA256_RE.match(value)
    # SKILL.md hash is the real sha256 of the bytes.
    expected = hashlib.sha256((repo / "agent_reach" / "skill" / "SKILL.md").read_bytes()).hexdigest()
    assert hashes["SKILL.md"] == expected


def test_hash_skill_payload_missing_core_raises(tmp_path):
    repo = tmp_path / "repo"
    (repo / "agent_reach" / "skill").mkdir(parents=True)
    with pytest.raises(vh.VetError):
        vh.hash_skill_payload(repo)


# ── constant extraction ──────────────────────────────────────────────────────

def test_extract_str_constant():
    src = 'FOO = "bar"\nOPENCLI_PACKAGE = "@jackwener/opencli"\n_X=\'y\'\n'
    assert vh.extract_str_constant(src, "OPENCLI_PACKAGE") == "@jackwener/opencli"
    assert vh.extract_str_constant(src, "_X") == "y"
    with pytest.raises(vh.VetError):
        vh.extract_str_constant(src, "MISSING")


# ── channel table + pin build/validate ───────────────────────────────────────

def _sample_channels():
    return vh.build_channels(
        opencli_pkg="@jackwener/opencli",
        opencli_ext_id="ildkmabpimmkaediidaifkhjpohdnifk",
        rdt_git_source="git+https://github.com/public-clis/rdt-cli.git@" + "b" * 40,
        npm_versions={"@jackwener/opencli": "1.8.6", "mcporter": "0.12.3"},
        pypi_versions={"twitter-cli": "0.8.5", "bilibili-cli": "0.6.2"},
    )


def test_build_channels_specs():
    channels = _sample_channels()
    assert channels["twitter"]["backends"][0] == {"kind": "pipx", "spec": "twitter-cli==0.8.5"}
    assert channels["bilibili"]["backends"][0] == {"kind": "pipx", "spec": "bilibili-cli==0.6.2"}
    assert channels["search"]["backends"][0] == {"kind": "npm", "spec": "mcporter@0.12.3"}
    # opencli npm backend everywhere it is used
    assert channels["facebook"]["backends"][0] == {"kind": "npm", "spec": "@jackwener/opencli@1.8.6"}
    # reddit carries opencli + the pinned rdt-cli git SHA
    kinds = [b.get("kind") for b in channels["reddit"]["backends"]]
    assert "npm" in kinds and "pipx-git" in kinds
    # opencli channel carries the manual Chrome-extension note (em-dash-free)
    notes = [b["note"] for b in channels["opencli"]["backends"] if "note" in b]
    assert any("ildkmabpimmkaediidaifkhjpohdnifk" in n for n in notes)
    assert all("—" not in n for n in notes)


def _sample_pin(**overrides):
    pin = vh.build_pin(
        upstream="https://github.com/Panniantong/Agent-Reach",
        commit_sha="e825f6740d24c6c315c3b0dc41907e6c87ff39a5",
        version_label="1.5.0",
        vetted_at="2026-07-11",
        constraints_file="agent-reach-constraints.txt",
        skill_hashes={"SKILL.md": "c" * 64, "references/dev.md": "d" * 64},
        channels=_sample_channels(),
    )
    pin.update(overrides)
    return pin


def test_build_pin_validate_and_json_roundtrip(tmp_path):
    pin = _sample_pin()
    vh.validate_pin(pin)  # must not raise

    out = vas.write_pin(pin, tmp_path)
    reloaded = json.loads(out.read_text(encoding="utf-8"))
    assert reloaded["schema_version"] == 1
    assert reloaded["commit_sha"] == pin["commit_sha"]
    vh.validate_pin(reloaded)  # round-trips cleanly


@pytest.mark.parametrize("bad_sha", ["v1.5.0", "main", "abc123", "E825F6740D24C6C315C3B0DC41907E6C87FF39A5"])
def test_validate_pin_rejects_non_sha(bad_sha):
    with pytest.raises(vh.VetError):
        vh.validate_pin(_sample_pin(commit_sha=bad_sha))


def test_validate_pin_rejects_missing_skill_md():
    with pytest.raises(vh.VetError):
        vh.validate_pin(_sample_pin(skill_hashes={"references/dev.md": "d" * 64}))


def test_validate_pin_rejects_bad_skill_hash():
    with pytest.raises(vh.VetError):
        vh.validate_pin(_sample_pin(skill_hashes={"SKILL.md": "not-a-hash"}))


def test_validate_pin_rejects_non_https_upstream():
    with pytest.raises(vh.VetError):
        vh.validate_pin(_sample_pin(upstream="http://insecure.example"))


@pytest.mark.parametrize(
    "channels",
    [
        {"x": {"backends": [{"kind": "npm", "spec": "mcporter"}]}},          # npm w/o version
        {"x": {"backends": [{"kind": "pipx", "spec": "twitter-cli"}]}},       # pipx w/o ==
        {"x": {"backends": [{"kind": "pipx-git", "spec": "twitter-cli==1"}]}},# not git+
        {"x": {"backends": [{"kind": "brew", "spec": "foo@1"}]}},             # bad kind
        {"x": {"backends": []}},                                              # empty
    ],
)
def test_validate_pin_rejects_malformed_channels(channels):
    with pytest.raises(vh.VetError):
        vh.validate_pin(_sample_pin(channels=channels))


def test_validate_pin_accepts_note_only_channel():
    channels = {"linkedin": {"backends": [{"note": "Manual setup; no auto-install."}]}}
    vh.validate_pin(_sample_pin(channels=channels))


# ── diff vs current pin ──────────────────────────────────────────────────────

def test_compute_diff_reports_files_deps_and_risk(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _write_skill(repo)
    _write_pyproject(repo, ["requests>=2.28", "feedparser>=6.0"])
    (repo / "agent_reach" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
    sha1 = _commit(repo, "v1")

    # Second commit: change a dep, add a subprocess call, edit an existing file.
    _write_pyproject(repo, ["requests>=2.30", "feedparser>=6.0", "yt-dlp>=2024.0"])
    (repo / "agent_reach" / "core.py").write_text(
        "import subprocess\nsubprocess.run(['curl', 'https://x'])\n", encoding="utf-8"
    )
    sha2 = _commit(repo, "v2")

    diff = vh.compute_diff(str(repo), sha1, sha2)

    assert "agent_reach/core.py" in diff.changed_files
    assert "pyproject.toml" in diff.changed_files
    # requests spec changed, yt-dlp added
    assert any("requests" in c for c in diff.dep_diff.changed)
    assert any("yt-dlp" in a for a in diff.dep_diff.added)
    # risk grep flagged the subprocess + curl lines in core.py
    assert "agent_reach/core.py" in diff.risk_hits
    labels = " ".join(diff.risk_hits["agent_reach/core.py"])
    assert "subprocess" in labels and "curl" in labels


def test_diff_deps_added_removed_changed():
    old = ["requests>=2.28", "feedparser>=6.0"]
    new = ["requests>=2.30", "yt-dlp>=2024.0"]
    dd = vh.diff_deps(old, new)
    assert any("yt-dlp" in a for a in dd.added)
    assert any("feedparser" in r for r in dd.removed)
    assert any("requests" in c for c in dd.changed)
    assert dd.has_changes()


def test_read_file_at_sha_missing_returns_none(tmp_path):
    repo = _init_repo(tmp_path / "repo")
    _write_skill(repo)
    _write_pyproject(repo, ["requests>=2.28"])
    sha = _commit(repo, "init")
    assert vh.read_file_at_sha(str(repo), sha, "does/not/exist.txt") is None
    assert vh.read_file_at_sha(str(repo), sha, "pyproject.toml") is not None


# ── network resolvers (stubbed) ──────────────────────────────────────────────

def test_resolve_npm_version_parses_stdout(monkeypatch):
    def fake_run(cmd, *, timeout):
        assert cmd[:2] == ["npm", "view"]
        return subprocess.CompletedProcess(cmd, 0, stdout="1.8.6\n", stderr="")

    monkeypatch.setattr(vas, "run_checked", fake_run)
    assert vas.resolve_npm_version("@jackwener/opencli") == "1.8.6"


def test_resolve_npm_version_empty_raises(monkeypatch):
    monkeypatch.setattr(
        vas, "run_checked",
        lambda cmd, *, timeout: subprocess.CompletedProcess(cmd, 0, stdout="\n", stderr=""),
    )
    with pytest.raises(vh.VetError):
        vas.resolve_npm_version("mcporter")


def test_resolve_pypi_version_parses_json(monkeypatch):
    class _FakeResp:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode()

        def read(self, *a):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        vas.urllib.request, "urlopen",
        lambda url, timeout=None: _FakeResp({"info": {"version": "0.8.5"}}),
    )
    assert vas.resolve_pypi_version("twitter-cli") == "0.8.5"


def test_resolve_channels_uses_extracted_constants(tmp_path, monkeypatch):
    # Minimal upstream layout with the three constants the vetter extracts.
    repo = tmp_path / "repo"
    (repo / "agent_reach" / "backends").mkdir(parents=True)
    (repo / "agent_reach" / "backends" / "opencli.py").write_text(
        'OPENCLI_PACKAGE = "@jackwener/opencli"\n'
        'OPENCLI_EXTENSION_ID = "ildkmabpimmkaediidaifkhjpohdnifk"\n',
        encoding="utf-8",
    )
    (repo / "agent_reach" / "cli.py").write_text(
        '_RDT_GIT_SOURCE = "git+https://github.com/public-clis/rdt-cli.git@%s"\n' % ("b" * 40),
        encoding="utf-8",
    )

    monkeypatch.setattr(vas, "resolve_npm_version", lambda pkg, **k: {"@jackwener/opencli": "1.8.6", "mcporter": "0.12.3"}[pkg])
    monkeypatch.setattr(vas, "resolve_pypi_version", lambda pkg, **k: {"twitter-cli": "0.8.5", "bilibili-cli": "0.6.2"}[pkg])

    channels = vas.resolve_channels(repo)
    assert channels["opencli"]["backends"][0]["spec"] == "@jackwener/opencli@1.8.6"
    assert channels["reddit"]["backends"][1]["spec"].startswith("git+https://github.com/public-clis/rdt-cli.git@")


def test_resolve_channels_layout_drift_raises_clean(tmp_path):
    # Upstream moved things: backends/opencli.py absent. Must fail loud (VetError),
    # not a raw traceback, so a bump PR forces a human re-check of the model.
    repo = tmp_path / "repo"
    (repo / "agent_reach").mkdir(parents=True)
    (repo / "agent_reach" / "cli.py").write_text("_RDT_GIT_SOURCE = \"git+x@y\"\n", encoding="utf-8")
    with pytest.raises(vh.VetError, match="layout differs"):
        vas.resolve_channels(repo)
