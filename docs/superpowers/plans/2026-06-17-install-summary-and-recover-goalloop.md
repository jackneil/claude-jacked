# jacked 0.51.0 — install change-summary + /recover goal-loop — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship 0.51.0 with (A) a concise, accurate install/upgrade change-summary (version before→after + skills/commands/agents/lenses/templates added/changed/removed) surfaced in the terminal and dashboard, backed by a new install manifest; and (B) `/recover` improvements: recommend the newest *substantive* session and surface a still-active `/goal`/`/loop` kickoff verbatim for manual restart.

**Architecture:** Part A introduces `jacked/install_manifest.py` (records what jacked installed, by content hash + version) and `jacked/install_summary.py` (builds the change record + renders the terminal summary). `install` diffs current source against the prior manifest, prunes removed artifacts, writes the manifest + a `jacked-last-install.json` record, and prints the concise summary instead of the old banner. A `GET /api/install/summary` route plus a one-shot dashboard panel and `update.html` rendering surface it in the UI. Part B extends `jacked/recover.py` with `recommend_index` (skip near-empty newest) and goal/loop kickoff extraction gated to "looks active".

**Tech Stack:** Python 3.10+, Click, rich, FastAPI (web), vanilla JS SPA (`jacked/data/web/`), pytest via `uv run python -m pytest`.

## Global Constraints

- **Run tests with** `uv run python -m pytest` — never bare `python -m pytest`.
- **Version:** bump `jacked/__init__.py` `__version__` to `0.51.0` (Task S1 only).
- **Manifest path** `~/.claude/jacked-manifest.json`; **last-install record** `~/.claude/jacked-last-install.json`. Both atomic-written (temp + rename).
- **Diff is by content hash of the source** (current source vs prior manifest), so it is correct in both copy and editable/symlink installs.
- **Prune is manifest-gated:** only delete a destination artifact whose name is in the prior manifest (jacked-owned). Never delete a file jacked didn't record installing. Prune failures log a warning, never abort install.
- **No silent banner swallow of blockers:** the verbose "What you get / Next steps / Recommended tools" banner is removed from default install output, but a genuinely-missing *required* plugin still prints a one-line warning.
- **Dashboard theme:** match existing dark-slate + blue (`#3b82f6`) using `.stat-card`/`.badge` from `jacked/data/web/css/style.css`. No cyan, no beta tag.
- **Part B detection:** `MIN_SUBSTANCE_MSGS = 4`, `TAIL_WINDOW = 10`. Goal/loop surfaced only when the latest `/goal`|`/loop` kickoff is within the last `TAIL_WINDOW` records of the recommended session. Detected commands: `/goal` and `/loop` only.
- **recover.py stays Qdrant-free** (imports only `jacked.transcript` + stdlib).

---

## File Structure

| File | Responsibility |
|---|---|
| `jacked/install_manifest.py` | **New.** `CATEGORIES`, `hash_source`, `load`, `diff` (`ManifestDiff`/`CategoryDiff`), `write`, `prune_removed`. Pure/stdlib. |
| `jacked/install_summary.py` | **New.** `build_record`, `render_terminal`, `write_last_install`. Pure (rich markup as plain strings). |
| `jacked/cli.py` | `install` wires diff→prune→manifest write→record write→summary render; removes banner (keeps required-plugin blocker); adds `--json`. `uninstall` made manifest-aware. `recover` uses `recommend_index`. |
| `jacked/api/routes/system.py` | **New route** `GET /api/install/summary`. |
| `jacked/data/web/js/components/install-summary.js` + wiring in `app.js`/`header.js` | One-shot "What changed" panel. |
| `jacked/data/web/update.html` | Render changes on `succeeded`. |
| `jacked/recover.py` | `recommend_index`; goal/loop extraction + active gate; `Digest.resumable_commands` + render. |
| `jacked/data/skills/recover/SKILL.md` | Note near-empty skip + manual-restart copy-paste step. |
| `tests/test_install_manifest.py`, `tests/test_install_summary.py`, `tests/test_recover.py`, `tests/test_api_install_summary.py` | Tests. |
| `jacked/__init__.py`, `README.md` | Version 0.51.0 + docs. |

---

## Task A1: `install_manifest.py`

**Files:**
- Create: `jacked/install_manifest.py`
- Test: `tests/test_install_manifest.py`

**Interfaces:**
- Produces: `CATEGORIES: list[Category]`; `hash_source(data_root) -> dict`; `load(path=DEFAULT_MANIFEST_PATH) -> dict | None`; `diff(prior, current) -> ManifestDiff`; `write(path, version, current_hashes, now_iso)`; `prune_removed(diff, home) -> list[str]`; dataclasses `Category`, `CategoryDiff`, `ManifestDiff`; `DEFAULT_MANIFEST_PATH`.

- [ ] **Step A1.1: Write the failing test**

```python
# tests/test_install_manifest.py
import json
from pathlib import Path

from jacked import install_manifest as m


def _make_source(root: Path):
    """Build a fake data_root with one of each artifact type."""
    (root / "skills" / "recover").mkdir(parents=True)
    (root / "skills" / "recover" / "SKILL.md").write_text("recover v1", encoding="utf-8")
    (root / "commands").mkdir()
    (root / "commands" / "dc.md").write_text("dc cmd", encoding="utf-8")
    (root / "agents").mkdir()
    (root / "agents" / "readme.md").write_text("agent", encoding="utf-8")
    (root / "lenses").mkdir()
    (root / "lenses" / "lens.md").write_text("lens", encoding="utf-8")
    (root / "templates").mkdir()
    (root / "templates" / "plan.html").write_text("<html>", encoding="utf-8")


def test_hash_source_keys_by_name(tmp_path):
    _make_source(tmp_path)
    h = m.hash_source(tmp_path)
    assert set(h) == {"skills", "commands", "agents", "lenses", "templates"}
    assert "recover" in h["skills"]            # skill keyed by dir name
    assert "dc.md" in h["commands"]            # file keyed by filename
    assert h["templates"]["plan.html"].startswith("sha256:")


def test_diff_added_changed_removed_unchanged(tmp_path):
    _make_source(tmp_path)
    current = m.hash_source(tmp_path)
    prior = {"version": "0.50.0", "artifacts": {
        "skills": {"recover": "sha256:OLD", "gone": "sha256:x"},  # recover changed, gone removed
        "commands": {"dc.md": current["commands"]["dc.md"]},      # unchanged
        "agents": {}, "lenses": {}, "templates": {},              # readme/lens/plan.html added
    }}
    d = m.diff(prior, current)
    assert d.by_category["skills"].changed == ["recover"]
    assert d.by_category["skills"].removed == ["gone"]
    assert d.by_category["commands"].unchanged == ["dc.md"]
    assert d.by_category["agents"].added == ["readme.md"]
    assert d.unchanged_count() == 1
    assert d.has_changes() is True


def test_diff_first_install_all_added(tmp_path):
    _make_source(tmp_path)
    d = m.diff(None, m.hash_source(tmp_path))
    assert d.by_category["skills"].added == ["recover"]
    assert d.by_category["skills"].removed == []


def test_load_missing_and_corrupt(tmp_path):
    assert m.load(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert m.load(bad) is None


def test_write_roundtrip(tmp_path):
    p = tmp_path / "manifest.json"
    m.write(p, "0.51.0", {"skills": {"recover": "sha256:a"}}, "2026-06-17T00:00:00Z")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["version"] == "0.51.0"
    assert data["artifacts"]["skills"]["recover"] == "sha256:a"


def test_prune_removed_deletes_only_listed(tmp_path):
    home = tmp_path
    # a jacked skill dir to be pruned, and a user's own skill that must survive
    (home / ".claude" / "skills" / "gone").mkdir(parents=True)
    (home / ".claude" / "skills" / "gone" / "SKILL.md").write_text("x", encoding="utf-8")
    (home / ".claude" / "skills" / "mine").mkdir(parents=True)
    (home / ".claude" / "skills" / "mine" / "SKILL.md").write_text("keep", encoding="utf-8")
    (home / ".claude" / "commands").mkdir(parents=True)
    (home / ".claude" / "commands" / "old.md").write_text("x", encoding="utf-8")
    d = m.ManifestDiff({
        "skills": m.CategoryDiff(removed=["gone"]),
        "commands": m.CategoryDiff(removed=["old.md"]),
        "agents": m.CategoryDiff(), "lenses": m.CategoryDiff(), "templates": m.CategoryDiff(),
    })
    pruned = m.prune_removed(d, home)
    assert not (home / ".claude" / "skills" / "gone").exists()
    assert not (home / ".claude" / "commands" / "old.md").exists()
    assert (home / ".claude" / "skills" / "mine").exists()   # untouched
    assert set(pruned) == {"skills/gone", "commands/old.md"}
```

- [ ] **Step A1.2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_install_manifest.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jacked.install_manifest'`.

- [ ] **Step A1.3: Write minimal implementation**

```python
# jacked/install_manifest.py
"""Manifest of jacked-installed artifacts, for change-summaries + safe pruning.

Records, per jacked version, the content hash of every artifact jacked ships
(skills/commands/agents/lenses/templates). Diffing the current source against
the prior manifest yields added/changed/removed/unchanged — correct in both
copy and editable/symlink installs (it compares source-now vs source-at-last-install).
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_MANIFEST_PATH = Path.home() / ".claude" / "jacked-manifest.json"


@dataclass(frozen=True)
class Category:
    key: str
    src_glob: str          # relative to data_root
    dest_subpath: str      # under ~/.claude
    is_skill_dir: bool = False

    def name_of(self, src_file: Path) -> str:
        return src_file.parent.name if self.is_skill_dir else src_file.name

    def prune_target(self, name: str, home: Path) -> Path:
        base = home / ".claude" / self.dest_subpath
        return base / name  # skill dir or file; both live directly under base


CATEGORIES = [
    Category("skills", "skills/*/SKILL.md", "skills", is_skill_dir=True),
    Category("commands", "commands/*.md", "commands"),
    Category("agents", "agents/*.md", "agents"),
    Category("lenses", "lenses/*.md", "lenses"),
    Category("templates", "templates/*.html", "jacked-templates"),
]


@dataclass
class CategoryDiff:
    added: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)


@dataclass
class ManifestDiff:
    by_category: dict  # key -> CategoryDiff

    def unchanged_count(self) -> int:
        return sum(len(c.unchanged) for c in self.by_category.values())

    def has_changes(self) -> bool:
        return any(c.added or c.changed or c.removed for c in self.by_category.values())

    def to_changes_dict(self) -> dict:
        return {
            k: {"added": c.added, "changed": c.changed, "removed": c.removed}
            for k, c in self.by_category.items()
        }


def _sha256_file(p: Path) -> str:
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


def hash_source(data_root) -> dict:
    data_root = Path(data_root)
    out: dict = {}
    for cat in CATEGORIES:
        names: dict = {}
        for src in sorted(data_root.glob(cat.src_glob)):
            try:
                names[cat.name_of(src)] = _sha256_file(src)
            except OSError as e:
                logger.warning("Could not hash %s: %s", src, e)
        out[cat.key] = names
    return out


def load(path=DEFAULT_MANIFEST_PATH) -> Optional[dict]:
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Ignoring unreadable manifest %s: %s", path, e)
        return None


def diff(prior: Optional[dict], current: dict) -> ManifestDiff:
    prior_arts = (prior or {}).get("artifacts", {})
    by_cat: dict = {}
    for cat in CATEGORIES:
        cur = current.get(cat.key, {})
        old = prior_arts.get(cat.key, {})
        cd = CategoryDiff()
        for name in sorted(cur):
            if name not in old:
                cd.added.append(name)
            elif old[name] != cur[name]:
                cd.changed.append(name)
            else:
                cd.unchanged.append(name)
        for name in sorted(old):
            if name not in cur:
                cd.removed.append(name)
        by_cat[cat.key] = cd
    return ManifestDiff(by_cat)


def write(path, version: str, current_hashes: dict, now_iso: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"version": version, "written_at": now_iso, "artifacts": current_hashes}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def prune_removed(d: ManifestDiff, home) -> list[str]:
    home = Path(home)
    pruned: list[str] = []
    cat_by_key = {c.key: c for c in CATEGORIES}
    for key, cd in d.by_category.items():
        cat = cat_by_key.get(key)
        if not cat:
            continue
        for name in cd.removed:
            target = cat.prune_target(name, home)
            try:
                if cat.is_skill_dir and target.is_dir():
                    shutil.rmtree(target)
                    pruned.append(f"{key}/{name}")
                elif target.is_symlink() or target.exists():
                    target.unlink()
                    pruned.append(f"{key}/{name}")
            except OSError as e:
                logger.warning("Could not prune %s: %s", target, e)
    return pruned
```

- [ ] **Step A1.4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_install_manifest.py -q`
Expected: PASS (6 passed).

- [ ] **Step A1.5: Commit**

```bash
git add jacked/install_manifest.py tests/test_install_manifest.py
git commit -m "feat(install): manifest module — hash/diff/write/prune jacked artifacts"
```

---

## Task A2: `install_summary.py`

**Files:**
- Create: `jacked/install_summary.py`
- Test: `tests/test_install_summary.py`

**Interfaces:**
- Consumes: `jacked.install_manifest.ManifestDiff` (Task A1).
- Produces: `build_record(diff, from_version, to_version, now_iso) -> dict`; `render_terminal(record) -> str`; `write_last_install(record, path=DEFAULT_LAST_INSTALL_PATH)`; `DEFAULT_LAST_INSTALL_PATH`.

- [ ] **Step A2.1: Write the failing test**

```python
# tests/test_install_summary.py
import json
from jacked import install_summary as s
from jacked.install_manifest import ManifestDiff, CategoryDiff


def _diff(**kw):
    cats = {k: CategoryDiff() for k in ("skills", "commands", "agents", "lenses", "templates")}
    for k, cd in kw.items():
        cats[k] = cd
    return ManifestDiff(cats)


def test_build_record_shape():
    d = _diff(skills=CategoryDiff(added=["recover"], unchanged=["whats-next"]))
    rec = s.build_record(d, "0.50.0", "0.51.0", "2026-06-17T00:00:00Z")
    assert rec["from_version"] == "0.50.0"
    assert rec["to_version"] == "0.51.0"
    assert rec["changes"]["skills"]["added"] == ["recover"]
    assert rec["unchanged_count"] == 1


def test_render_upgrade_shows_arrow_and_changes():
    d = _diff(skills=CategoryDiff(added=["recover"]),
              commands=CategoryDiff(changed=["whats-next.md"]),
              agents=CategoryDiff(removed=["legacy.md"], unchanged=["a.md", "b.md"]))
    rec = s.build_record(d, "0.50.0", "0.51.0", "2026-06-17T00:00:00Z")
    out = s.render_terminal(rec)
    assert "0.50.0" in out and "0.51.0" in out and "→" in out
    assert "recover" in out and "whats-next.md" in out and "legacy.md" in out
    assert "Restart Claude Code" in out


def test_render_first_install_no_from_version():
    d = _diff(skills=CategoryDiff(added=["recover"]))
    rec = s.build_record(d, None, "0.51.0", "2026-06-17T00:00:00Z")
    out = s.render_terminal(rec)
    assert "installed" in out.lower()
    assert "0.51.0" in out


def test_render_no_changes_says_up_to_date():
    d = _diff(skills=CategoryDiff(unchanged=["recover", "whats-next"]))
    rec = s.build_record(d, "0.51.0", "0.51.0", "2026-06-17T00:00:00Z")
    out = s.render_terminal(rec)
    assert "up to date" in out.lower()


def test_write_last_install_roundtrip(tmp_path):
    rec = {"at": "x", "from_version": None, "to_version": "0.51.0",
           "changes": {}, "unchanged_count": 0}
    p = tmp_path / "last.json"
    s.write_last_install(rec, p)
    assert json.loads(p.read_text(encoding="utf-8"))["to_version"] == "0.51.0"
```

- [ ] **Step A2.2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_install_summary.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jacked.install_summary'`.

- [ ] **Step A2.3: Write minimal implementation**

```python
# jacked/install_summary.py
"""Build + render the install/upgrade change-summary, and persist the record
the dashboard reads (~/.claude/jacked-last-install.json)."""
from __future__ import annotations

import json
from pathlib import Path

from jacked.install_manifest import ManifestDiff

DEFAULT_LAST_INSTALL_PATH = Path.home() / ".claude" / "jacked-last-install.json"

_LABELS = [
    ("skills", "Skills"), ("commands", "Commands"), ("agents", "Agents"),
    ("lenses", "Lenses"), ("templates", "Templates"),
]
_SYM = {
    "added": ("+", "new", "green"),
    "changed": ("~", "updated", "yellow"),
    "removed": ("−", "removed", "red"),
}


def build_record(diff: ManifestDiff, from_version, to_version: str, now_iso: str) -> dict:
    return {
        "at": now_iso,
        "from_version": from_version,
        "to_version": to_version,
        "changes": diff.to_changes_dict(),
        "unchanged_count": diff.unchanged_count(),
    }


def _has_changes(record: dict) -> bool:
    return any(
        ch.get(k) for ch in record["changes"].values() for k in ("added", "changed", "removed")
    )


def render_terminal(record: dict) -> str:
    frm, to = record["from_version"], record["to_version"]
    changed = _has_changes(record)
    lines: list[str] = []

    if frm is None:
        lines.append(f"[bold]Jacked installed[/bold]  —  {to}")
    elif frm != to:
        lines.append(f"[bold]Jacked upgraded[/bold]   {frm} → {to}")
    elif changed:
        lines.append(f"[bold]Jacked {to}[/bold] — files refreshed")
    else:
        return f"[green]Jacked {to}[/green] — already up to date ({record['unchanged_count']} artifacts unchanged)"

    lines.append("")
    for cat_key, label in _LABELS:
        ch = record["changes"].get(cat_key, {})
        for kind in ("added", "changed", "removed"):
            for name in ch.get(kind, []):
                sym, word, color = _SYM[kind]
                lines.append(f"  {label:<11}[{color}]{sym}[/{color}] {name:<28}{word}")
    if not changed:
        lines.append("  (no artifact changes)")
    lines.append(f"            {record['unchanged_count']} unchanged")
    lines.append("")
    lines.append("→ Restart Claude Code to load changes.")
    return "\n".join(lines)


def write_last_install(record: dict, path=DEFAULT_LAST_INSTALL_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    tmp.replace(path)
```

- [ ] **Step A2.4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_install_summary.py -q`
Expected: PASS (5 passed).

- [ ] **Step A2.5: Commit**

```bash
git add jacked/install_summary.py tests/test_install_summary.py
git commit -m "feat(install): build + render change-summary record"
```

---

## Task A3: Wire the summary into `install` (and make `uninstall` manifest-aware)

**Files:**
- Modify: `jacked/cli.py` — the `install` command (`def install`, ~line 2723) and `uninstall` (~line 3273)
- Test: `tests/test_install_summary.py` (append a CliRunner integration test)

**Interfaces:**
- Consumes: `jacked.install_manifest` (A1), `jacked.install_summary` (A2), existing `_get_data_root()`, `_install_asset_dir`, the skills loop, `from jacked import __version__`.
- Produces: install behavior — writes manifest + `jacked-last-install.json`, prints the concise summary, prunes removed artifacts, supports `--json`; banner removed (required-plugin blocker kept). No new public Python symbols.

**Implementer guidance:** First READ `jacked/cli.py` `install` (≈2723-3147) and `uninstall` (≈3273-3432). Keep the existing artifact-installation loops (`_install_asset_dir`, the skills `glob` loop, `_link_or_copy`) — they still do the copying. ADD the manifest orchestration AROUND them and REPLACE the trailing banner. Honor `CLAUDE_PROJECTS_DIR`-style testability: the manifest/last-install paths must be overridable via env (`JACKED_HOME` → default `Path.home()`), so add a tiny helper `_jacked_home()` returning `Path(os.getenv("JACKED_HOME") or Path.home())` and use it for manifest + last-install + `home` in install (the test relies on this).

- [ ] **Step A3.1: Write the failing test**

```python
# append to tests/test_install_summary.py
from click.testing import CliRunner
from jacked.cli import main


def test_cli_install_writes_manifest_and_summary(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("JACKED_HOME", str(fake_home))
    runner = CliRunner()
    # First install: everything new, manifest + last-install written.
    r1 = runner.invoke(main, ["install", "--no-security", "--no-rules"])
    assert r1.exit_code == 0, r1.output
    assert (fake_home / ".claude" / "jacked-manifest.json").exists()
    last = fake_home / ".claude" / "jacked-last-install.json"
    assert last.exists()
    import json
    rec = json.loads(last.read_text(encoding="utf-8"))
    assert rec["from_version"] is None
    assert "recover" in rec["changes"]["skills"]["added"]
    assert "installed" in r1.output.lower()
    # Old banner must be gone.
    assert "What you get" not in r1.output

    # Second install (no version change): up to date, no spurious changes.
    r2 = runner.invoke(main, ["install", "--no-security", "--no-rules"])
    assert r2.exit_code == 0, r2.output
    assert "up to date" in r2.output.lower()


def test_cli_install_json_emits_record(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("JACKED_HOME", str(fake_home))
    runner = CliRunner()
    r = runner.invoke(main, ["install", "--no-security", "--no-rules", "--json"])
    assert r.exit_code == 0, r.output
    import json
    payload = json.loads(r.output.strip().splitlines()[-1])
    assert payload["to_version"]
    assert "skills" in payload["changes"]
```

- [ ] **Step A3.2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_install_summary.py -q`
Expected: FAIL — manifest/last-install not written, or `--json` unknown option.

- [ ] **Step A3.3: Write minimal implementation**

In `jacked/cli.py`:

1. Add near the other helpers:
```python
def _jacked_home() -> Path:
    import os as _os
    return Path(_os.getenv("JACKED_HOME") or Path.home())
```

2. Add `--json` to the `install` command options:
```python
@click.option("--json", "as_json", is_flag=True, help="Emit the change-summary as JSON instead of the human summary")
```
and add `as_json: bool` to the `def install(...)` signature.

3. Replace `home = Path.home()` at the top of `install` with `home = _jacked_home()`.

4. At the START of `install` (right after computing `pkg_root = _get_data_root()`), capture the prior manifest + version:
```python
    from jacked import install_manifest as _mani
    from jacked import install_summary as _isum
    from jacked import __version__ as _ver
    from datetime import datetime, timezone
    _manifest_path = home / ".claude" / "jacked-manifest.json"
    _prior_manifest = _mani.load(_manifest_path)
    _prior_version = _prior_manifest.get("version") if _prior_manifest else None
```

5. Keep ALL existing artifact-install loops as they are (they still copy/symlink). When `as_json` is True, suppress the existing per-line `console.print("[green][OK]...")` chatter — wrap those prints in `if not as_json:` (or set `console.quiet = True` for the duration). Minimal approach: guard the per-category `[OK]` prints with `if not as_json:`.

6. REPLACE the entire trailing banner block (from `console.print("\n[bold]Installation complete![/bold]")` through the end of `_recommend_external_tools()` call, ≈ cli.py 3000-3147) with:
```python
    # --- Change summary (manifest-driven) ---
    _current_hashes = _mani.hash_source(pkg_root)
    _d = _mani.diff(_prior_manifest, _current_hashes)
    _pruned = _mani.prune_removed(_d, home)
    _now = datetime.now(timezone.utc).isoformat()
    _mani.write(_manifest_path, _ver, _current_hashes, _now)
    _record = _isum.build_record(_d, _prior_version, _ver, _now)
    _isum.write_last_install(_record, home / ".claude" / "jacked-last-install.json")

    if as_json:
        import json as _json
        click.echo(_json.dumps(_record))
    else:
        console.print("")
        console.print(_isum.render_terminal(_record))
        # Required-plugin blocker only (full recommendations now live in `jacked doctor`):
        _warn_required_plugins_missing()
```

7. Add `_warn_required_plugins_missing()` — extract ONLY the *required* (not "recommended"/"optional") plugin-missing checks from the existing `_recommend_external_tools()` and print a single yellow warning line per genuinely-missing required plugin; print nothing if all required plugins are present. Move the remainder of `_recommend_external_tools` behind the existing `doctor` command (call it from `doctor`); leave `_recommend_external_tools` defined so `doctor` can call it.

8. **`uninstall` manifest-aware:** after the existing source-glob removal, also delete any artifact still recorded in the manifest but no longer in source (covers pruned-then-reinstalled history), then remove the manifest + last-install files:
```python
    _mp = _jacked_home() / ".claude" / "jacked-manifest.json"
    if _mp.exists():
        _mp.unlink()
    _lp = _jacked_home() / ".claude" / "jacked-last-install.json"
    if _lp.exists():
        _lp.unlink()
```

- [ ] **Step A3.4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_install_summary.py -q`
Expected: PASS. Then manually: `JACKED_HOME=$(mktemp -d) uv run jacked install --no-security --no-rules` shows the concise summary, no "What you get" banner.

- [ ] **Step A3.5: Commit**

```bash
git add jacked/cli.py tests/test_install_summary.py
git commit -m "feat(install): manifest-driven change summary replaces banner; --json; prune; manifest-aware uninstall"
```

---

## Task A4: `GET /api/install/summary`

**Files:**
- Modify: `jacked/api/routes/system.py` (add a route on the existing `router`)
- Test: `tests/test_api_install_summary.py`

**Interfaces:**
- Consumes: `jacked.install_summary.DEFAULT_LAST_INSTALL_PATH`.
- Produces: route `GET /api/install/summary` → `{"summary": <record|null>, "mtime_iso": <iso|null>}`.

**Implementer guidance:** READ `jacked/api/routes/system.py` to confirm the `router` object and how existing routes (e.g. `/update/status`) read JSON files, and match that style.

- [ ] **Step A4.1: Write the failing test**

```python
# tests/test_api_install_summary.py
import json
from fastapi.testclient import TestClient
from jacked.api.main import app


def test_install_summary_absent(tmp_path, monkeypatch):
    monkeypatch.setattr("jacked.install_summary.DEFAULT_LAST_INSTALL_PATH", tmp_path / "missing.json")
    # Re-point the route's module-level reference if it imported the constant directly:
    import jacked.api.routes.system as sysroutes
    monkeypatch.setattr(sysroutes, "_LAST_INSTALL_PATH", tmp_path / "missing.json", raising=False)
    client = TestClient(app)
    r = client.get("/api/install/summary")
    assert r.status_code == 200
    assert r.json()["summary"] is None


def test_install_summary_present(tmp_path, monkeypatch):
    p = tmp_path / "last.json"
    p.write_text(json.dumps({"to_version": "0.51.0", "from_version": "0.50.0",
                             "changes": {}, "unchanged_count": 3, "at": "x"}), encoding="utf-8")
    import jacked.api.routes.system as sysroutes
    monkeypatch.setattr(sysroutes, "_LAST_INSTALL_PATH", p, raising=False)
    client = TestClient(app)
    r = client.get("/api/install/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["to_version"] == "0.51.0"
    assert body["mtime_iso"]
```

- [ ] **Step A4.2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_api_install_summary.py -q`
Expected: FAIL — 404 (route not registered).

- [ ] **Step A4.3: Write minimal implementation**

In `jacked/api/routes/system.py` add (near the `/update/status` route):
```python
from jacked.install_summary import DEFAULT_LAST_INSTALL_PATH as _LAST_INSTALL_PATH


@router.get("/install/summary")
async def get_install_summary():
    import json
    from datetime import datetime, timezone
    p = _LAST_INSTALL_PATH
    try:
        if not p.exists():
            return {"summary": None, "mtime_iso": None}
        summary = json.loads(p.read_text(encoding="utf-8"))
        mtime_iso = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()
        return {"summary": summary, "mtime_iso": mtime_iso}
    except (OSError, json.JSONDecodeError):
        return {"summary": None, "mtime_iso": None}
```
(Use a module-level `_LAST_INSTALL_PATH` so tests can monkeypatch it.)

- [ ] **Step A4.4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_api_install_summary.py -q`
Expected: PASS (2 passed).

- [ ] **Step A4.5: Commit**

```bash
git add jacked/api/routes/system.py tests/test_api_install_summary.py
git commit -m "feat(api): GET /api/install/summary serves the last-install change record"
```

---

## Task A5: Dashboard "What changed" panel + update.html

**Files:**
- Create: `jacked/data/web/js/components/install-summary.js`
- Modify: `jacked/data/web/js/app.js` (init wiring), `jacked/data/web/index.html` (script tag if components are listed there), `jacked/data/web/update.html` (render on success), `jacked/data/web/css/style.css` (only if existing classes insufficient)

**Interfaces:**
- Consumes: `GET /api/install/summary` (Task A4).
- Produces: a one-shot post-upgrade summary panel; no Python.

**Implementer guidance:** This is the only non-unit-tested task — verify via `/qa`. READ `jacked/data/web/js/components/header.js` (upgrade modal completion, `_startHealthPolling`/reload), `app.js` (init + how components are registered/loaded), `index.html` (`#toast-container`, `#content`, how `js/components/*.js` are included), and `css/style.css` (`.stat-card`, `.badge`, `.badge-success/-warning/-danger`, `.upgrade-modal*`). Match the existing vanilla-JS module pattern exactly — do not introduce a framework or build step.

Behavior to implement:
1. On app init (and after the post-upgrade reload), call `GET /api/install/summary`.
2. If `summary` is non-null AND `summary.at` differs from `localStorage.getItem('jacked:lastInstallSeen')` AND the summary has any added/changed/removed, render a dismissible panel (reuse `.stat-card` container + `.badge-success` for added, `.badge-warning` for changed, `.badge-danger` for removed) titled `Updated NN → NN` (or `Installed — NN`). List each artifact `category: name` with its colored badge, plus `N unchanged`.
3. On render, set `localStorage.setItem('jacked:lastInstallSeen', summary.at)` so it shows once.
4. `update.html`: in the success branch (`overall === 'succeeded'`, see its `renderBanner`), `fetch('/api/install/summary')` and append the added/changed/removed lists using update.html's own inline dark styles.

- [ ] **Step A5.1: Implement the component + wiring**

Write `install-summary.js` following the existing component pattern, wire it into `app.js` init, and extend `update.html`. (No unit test — JS has no harness here.)

- [ ] **Step A5.2: QA the panel**

Simulate a record then load the dashboard:
```bash
# Write a fake record, start nothing (user runs the dashboard separately):
JH=$(mktemp -d); mkdir -p "$JH/.claude"
cat > "$JH/.claude/jacked-last-install.json" <<'JSON'
{"at":"2026-06-17T00:00:00Z","from_version":"0.50.0","to_version":"0.51.0","changes":{"skills":{"added":["recover"],"changed":[],"removed":[]},"commands":{"added":[],"changed":["whats-next.md"],"removed":[]},"agents":{"added":[],"changed":[],"removed":[]},"lenses":{"added":[],"changed":[],"removed":[]},"templates":{"added":[],"changed":[],"removed":[]}},"unchanged_count":34}
JSON
echo "Then run the dashboard with JACKED_HOME=$JH and /qa the panel."
```
Run `/qa` against the dashboard URL: confirm the panel renders the version arrow + `recover (new, green)` + `whats-next.md (updated, yellow)` + `34 unchanged`, matches slate/blue theme, shows once (reload → gone), and no console errors.

- [ ] **Step A5.3: Commit**

```bash
git add jacked/data/web/
git commit -m "feat(web): one-shot post-upgrade change-summary panel + update.html rendering"
```

---

## Task B1: `recover.py` — recommend the newest substantive session

**Files:**
- Modify: `jacked/recover.py`, `jacked/cli.py` (recover command `chosen`)
- Test: `tests/test_recover.py`

**Interfaces:**
- Consumes: `SessionCandidate` (existing).
- Produces: `recommend_index(candidates, min_msgs=MIN_SUBSTANCE_MSGS) -> int`; constant `MIN_SUBSTANCE_MSGS = 4`.

- [ ] **Step B1.1: Write the failing test**

```python
# append to tests/test_recover.py
def _cand(session_id, msg_count, last_ts):
    return rec.SessionCandidate(
        session_id=session_id, path=Path(f"/x/{session_id}.jsonl"),
        last_ts=datetime.fromisoformat(last_ts), msg_count=msg_count)


def test_recommend_skips_near_empty_newest():
    cands = [
        _cand("new-empty", 1, "2026-06-17T12:00:00+00:00"),   # newest but tiny
        _cand("substantive", 40, "2026-06-16T09:00:00+00:00"),
    ]
    assert rec.recommend_index(cands) == 1


def test_recommend_takes_newest_when_substantive():
    cands = [
        _cand("new-big", 12, "2026-06-17T12:00:00+00:00"),
        _cand("old", 40, "2026-06-16T09:00:00+00:00"),
    ]
    assert rec.recommend_index(cands) == 0


def test_recommend_falls_back_when_all_tiny():
    cands = [_cand("a", 1, "2026-06-17T12:00:00+00:00"),
             _cand("b", 2, "2026-06-16T09:00:00+00:00")]
    assert rec.recommend_index(cands) == 0
```

- [ ] **Step B1.2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_recover.py -q`
Expected: FAIL — `AttributeError: module 'jacked.recover' has no attribute 'recommend_index'`.

- [ ] **Step B1.3: Write minimal implementation**

Add to `jacked/recover.py`:
```python
MIN_SUBSTANCE_MSGS = 4


def recommend_index(candidates, min_msgs: int = MIN_SUBSTANCE_MSGS) -> int:
    """Index of the recommended candidate: the newest with real substance.
    Candidates are newest-first; skip a near-empty newest, else fall back to 0."""
    for i, c in enumerate(candidates):
        if c.msg_count >= min_msgs:
            return i
    return 0
```

In `jacked/cli.py` `recover` (phase 1), change the chosen selection:
```python
    idx = rec.recommend_index(candidates) if candidates else 0
    chosen = candidates[idx] if candidates else None
    top = candidates[:limit]
    # ensure the recommended candidate is present in the returned list
    if chosen is not None and chosen not in top:
        top = [chosen] + top[: max(0, limit - 1)]
```
and set `"chosen": chosen.to_dict(now) if chosen else None` (was `top[0]`).

- [ ] **Step B1.4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_recover.py -q`
Expected: PASS.

- [ ] **Step B1.5: Commit**

```bash
git add jacked/recover.py jacked/cli.py tests/test_recover.py
git commit -m "feat(recover): recommend newest substantive session (skip near-empty)"
```

---

## Task B2: `recover.py` — surface a still-active /goal or /loop kickoff

**Files:**
- Modify: `jacked/recover.py`
- Test: `tests/test_recover.py`

**Interfaces:**
- Consumes: `_iter_records`, `build_digest`/`Digest`, `render_digest` (existing).
- Produces: `Digest.resumable_commands: list[dict]` (each `{"type","kickoff"}`); constant `TAIL_WINDOW = 10`; helpers `_extract_kickoffs(path) -> tuple[list, int]`, `_active_kickoffs(path, tail_window=TAIL_WINDOW) -> list[dict]`.

- [ ] **Step B2.1: Write the failing test**

```python
# append to tests/test_recover.py
def _loop_user_line(args="5m /babysit-prs", ts="2026-06-16T09:00:00.000Z"):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content":
            f"<command-name>/loop</command-name><command-args>{args}</command-args>"}}


def _plain_user(text, ts="2026-06-16T09:10:00.000Z"):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}


def test_active_loop_kickoff_surfaced_when_in_tail(tmp_path):
    pdir = tmp_path / "p"
    path = _write_session(pdir, SID_A, [
        _user_line("/repo", ts="2026-06-16T08:00:00.000Z"),
        _loop_user_line(),  # near the end -> active
    ])
    d = rec.build_digest(path)
    assert d.resumable_commands == [{"type": "loop", "kickoff": "/loop 5m /babysit-prs"}]
    rendered = rec.render_digest(d)
    assert "Manual restart required" in rendered
    assert "/loop 5m /babysit-prs" in rendered


def test_goal_loop_not_surfaced_when_early_with_later_work(tmp_path):
    pdir = tmp_path / "p"
    records = [_user_line("/repo", ts="2026-06-16T08:00:00.000Z"), _loop_user_line()]
    # bury the kickoff under > TAIL_WINDOW later messages
    for i in range(rec.TAIL_WINDOW + 3):
        records.append(_plain_user(f"more work {i}", ts="2026-06-16T09:%02d:00.000Z" % (i % 60)))
    path = _write_session(pdir, SID_B, records)
    d = rec.build_digest(path)
    assert d.resumable_commands == []
    assert "Manual restart required" not in rec.render_digest(d)


def test_goal_kickoff_from_raw_slash_line(tmp_path):
    pdir = tmp_path / "p"
    path = _write_session(pdir, SID_A, [
        _user_line("/repo", ts="2026-06-16T08:00:00.000Z"),
        _plain_user("/goal ship the recover feature end to end"),
    ])
    d = rec.build_digest(path)
    assert d.resumable_commands == [{"type": "goal", "kickoff": "/goal ship the recover feature end to end"}]
```

- [ ] **Step B2.2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_recover.py -q`
Expected: FAIL — `Digest` has no `resumable_commands` / `AttributeError`.

- [ ] **Step B2.3: Write minimal implementation**

In `jacked/recover.py`:

1. Add imports + constants near the top (re is already needed; add if missing):
```python
import re

TAIL_WINDOW = 10
_GOAL_LOOP = ("goal", "loop")
_CN_RE = re.compile(r"<command-name>\s*/?([a-z0-9-]+)\s*</command-name>", re.IGNORECASE)
_CA_RE = re.compile(r"<command-args>(.*?)</command-args>", re.IGNORECASE | re.DOTALL)
_RAW_CMD_RE = re.compile(r"^/(goal|loop)\b", re.IGNORECASE)
```

2. Add the `resumable_commands` field to `Digest` (default empty list):
```python
    resumable_commands: list[dict] = field(default_factory=list)
```

3. Add helpers:
```python
def _raw_user_text(rec_obj: dict) -> str:
    content = (rec_obj.get("message") or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif isinstance(b, str):
                parts.append(b)
        return " ".join(parts)
    return ""


def _parse_goal_loop(raw: str):
    """Return (type, verbatim_kickoff) if raw invokes /goal or /loop, else (None, None)."""
    m = _CN_RE.search(raw)
    if m and m.group(1).lower() in _GOAL_LOOP:
        name = m.group(1).lower()
        args_m = _CA_RE.search(raw)
        args = args_m.group(1).strip() if args_m else ""
        return name, (f"/{name} {args}".rstrip())
    stripped = raw.strip()
    rm = _RAW_CMD_RE.match(stripped)
    if rm:
        name = rm.group(1).lower()
        return name, stripped.splitlines()[0].strip()
    return None, None


def _extract_kickoffs(path):
    """Return (kickoffs, total_records). Each kickoff: (index, type, verbatim)."""
    kickoffs = []
    total = 0
    for i, rec_obj in enumerate(_iter_records(path)):
        total = i + 1
        if not isinstance(rec_obj, dict) or rec_obj.get("type") != "user":
            continue
        raw = _raw_user_text(rec_obj)
        if not raw:
            continue
        kind, kickoff = _parse_goal_loop(raw)
        if kind:
            kickoffs.append((i, kind, kickoff))
    return kickoffs, total


def _active_kickoffs(path, tail_window: int = TAIL_WINDOW) -> list[dict]:
    """Surface the latest /goal|/loop kickoff only if it falls in the transcript tail."""
    kickoffs, total = _extract_kickoffs(path)
    if not kickoffs:
        return []
    last_idx, kind, kickoff = kickoffs[-1]
    if last_idx >= total - tail_window:
        return [{"type": kind, "kickoff": kickoff}]
    return []
```

4. In `build_digest`, populate the field (add before constructing `Digest`):
```python
    resumable = _active_kickoffs(session_path)
```
and pass `resumable_commands=resumable` into the `Digest(...)` constructor.

5. In `render_digest`, prepend the manual-restart block (right after the header section, before "Last instruction") — it is tiny and must never be budgeted out:
```python
    if digest.resumable_commands:
        rc = digest.resumable_commands[0]
        sections.insert(1, ("⚠ Manual restart required",
            f"This session was driving a /{rc['type']} that can't be auto-resumed.\n"
            f"Copy this back into Claude Code to restart it:\n\n    {rc['kickoff']}"))
```
(Insert at index 1 so it sits just under the header block; adjust to your section-list construction so it lands near the top and is always emitted.)

- [ ] **Step B2.4: Run test to verify it passes**

Run: `uv run python -m pytest tests/test_recover.py -q`
Expected: PASS.

- [ ] **Step B2.5: Commit**

```bash
git add jacked/recover.py tests/test_recover.py
git commit -m "feat(recover): surface still-active /goal|/loop kickoff for manual restart"
```

---

## Task B3: Update the `/recover` skill

**Files:**
- Modify: `jacked/data/skills/recover/SKILL.md`

**Interfaces:** none (Markdown guidance).

- [ ] **Step B3.1: Edit the skill**

Add to the recommendation/presentation guidance (after the existing step that presents the auto-pick): a sentence noting the recommended pick is the newest *substantive* session — a near-empty newest is skipped but still listed as an alternate. And add a step after the digest-injection step:

```markdown
## Manual restart of /goal or /loop
If the recovered digest contains a "Manual restart required" block, the crashed session was driving a `/goal` or `/loop` that **cannot be auto-resumed**. Surface that exact command to the user and tell them to paste it into Claude Code themselves to restart it — do not try to run it yourself.
```

- [ ] **Step B3.2: Verify the skill still reads coherently**

Run: `sed -n '1,80p' jacked/data/skills/recover/SKILL.md` and confirm the new sections are well-placed and the description frontmatter is unchanged.

- [ ] **Step B3.3: Commit**

```bash
git add jacked/data/skills/recover/SKILL.md
git commit -m "docs(recover): skill notes near-empty skip + /goal|/loop manual restart"
```

---

## Task S1: Version bump, README, full verification

**Files:**
- Modify: `jacked/__init__.py`, `README.md`

- [ ] **Step S1.1: Bump version**

In `jacked/__init__.py` set `__version__ = "0.51.0"` (confirm current first: `grep __version__ jacked/__init__.py`).

- [ ] **Step S1.2: README**

Add a short entry documenting (a) the install/upgrade change-summary (version before→after + added/changed/removed skills/commands/agents; works in terminal and dashboard) and (b) `/recover` now recommends the newest substantive session and surfaces a still-active `/goal`/`/loop` command to copy-paste.

- [ ] **Step S1.3: Full test suite**

Run: `uv run python -m pytest -q`
Expected: all pass, no regressions (prior baseline 2315 passed + the new tests).

- [ ] **Step S1.4: E2E smoke (terminal summary against a temp home)**

```bash
JH=$(mktemp -d)
JACKED_HOME=$JH uv run jacked install --no-security --no-rules    # first: everything "new", no banner
JACKED_HOME=$JH uv run jacked install --no-security --no-rules    # second: "already up to date"
cat "$JH/.claude/jacked-manifest.json" | python -m json.tool | head
```
Expected: first run prints the concise "installed" summary listing skills/commands/etc as new and writes the manifest + last-install; second run prints "already up to date".

- [ ] **Step S1.5: Commit**

```bash
git add jacked/__init__.py README.md
git commit -m "chore: bump to 0.51.0 + document install summary and /recover goal-loop"
```

---

## Self-Review

**Spec coverage:** A1 manifest ✔ (A1); A2 install diff/prune/summary/--json ✔ (A2,A3); A3 dashboard surface ✔ (A4 route, A5 panel+update.html); B1 near-empty recommend ✔ (B1); B2 goal/loop active-gated kickoff ✔ (B2); B3 skill ✔ (B3); version/README/testing ✔ (S1). Banner-removal-with-blocker-exception ✔ (A3 step 6-7). Theme-match ✔ (A5 guidance).

**Placeholder scan:** Python tasks carry complete code. A5 (JS) is intentionally guidance+sketch+QA (no JS unit harness exists; writing-plans allows following existing patterns for UI) — it names exact files, the endpoint, the styling classes, and a concrete behavior spec, not "TBD".

**Type consistency:** `ManifestDiff`/`CategoryDiff`/`diff`/`hash_source`/`write`/`prune_removed` identical across A1↔A2↔A3. `build_record`/`render_terminal`/`write_last_install` identical A2↔A3. `recommend_index`/`MIN_SUBSTANCE_MSGS` A B1↔cli. `resumable_commands`/`TAIL_WINDOW`/`_active_kickoffs` B2↔tests. `_LAST_INSTALL_PATH` A4 route↔tests. `_jacked_home`/`JACKED_HOME` A3↔tests.
