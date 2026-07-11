# scripts/_vet_helpers.py
"""Pure, offline-testable helpers for the Agent-Reach vetting pipeline.

Everything network-touching (git clone, uv pip compile, pip-audit, npm view,
PyPI JSON) lives in vet_agent_reach.py. This module holds the deterministic
pieces the unit tests exercise directly: ref->SHA resolution, skill hashing,
pin-file assembly + schema validation, and the diff-vs-current-pin logic.

No shell=True anywhere. Every subprocess result is checked explicitly and
raises VetError with captured stderr rather than trusting a bare exit code.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# ── Constants ────────────────────────────────────────────────────────────────

#: A vetted pin is ALWAYS a full 40-char lowercase commit SHA, never a tag or
#: branch name (tags are mutable and are the V1 poison vector).
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: Bare lowercase sha256 hex (skill-payload hashes are stored in this form).
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: Valid backend kinds in the pin file's channel table.
VALID_BACKEND_KINDS = ("npm", "pipx", "pipx-git")

#: Grep heuristics flagged in changed files during a bump diff. These are not a
#: security verdict; they steer the human reviewer to the code worth reading.
RISK_PATTERNS = (
    ("subprocess", re.compile(r"\bsubprocess\b")),
    ("os.system", re.compile(r"\bos\.system\b")),
    ("requests.", re.compile(r"\brequests\.")),
    ("urllib", re.compile(r"\burllib\b")),
    ("curl", re.compile(r"\bcurl\b")),
    ("npm install", re.compile(r"npm\s+(install|i)\b")),
    ("pip install", re.compile(r"pip\s+install\b")),
)

#: File suffixes worth scanning for risk patterns (skip binaries/assets).
_SCANNABLE_SUFFIXES = (".py", ".sh", ".js", ".ts", ".mjs", ".toml", ".txt", ".yaml", ".yml", ".md")


class VetError(RuntimeError):
    """A vetting step failed in a way that must stop the run loudly."""


# ── Subprocess ───────────────────────────────────────────────────────────────

def run_checked(
    cmd: list[str],
    *,
    cwd: str | Path | None = None,
    timeout: float,
    allow_fail: bool = False,
) -> subprocess.CompletedProcess:
    """Run an explicit arg list (never shell) and check the result.

    Raises VetError with captured stderr on non-zero exit unless allow_fail is
    set. Never trust a bare exit code for a multi-step op — callers post-verify.
    """
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise VetError(f"command not found: {cmd[0]!r} ({exc})") from exc
    except subprocess.TimeoutExpired as exc:
        raise VetError(f"command timed out after {timeout}s: {' '.join(cmd)}") from exc
    if proc.returncode != 0 and not allow_fail:
        stderr = (proc.stderr or "").strip() or (proc.stdout or "").strip()
        raise VetError(
            f"command failed (exit {proc.returncode}): {' '.join(cmd)}\n{stderr}"
        )
    return proc


# ── Ref -> SHA ───────────────────────────────────────────────────────────────

def _validate_sha(sha: str) -> str:
    sha = sha.strip().lower()
    if not SHA_RE.match(sha):
        raise VetError(f"resolved value is not a full 40-char commit SHA: {sha!r}")
    return sha


def resolve_ref_to_sha(upstream: str, ref: str, *, timeout: float = 30.0) -> str:
    """Resolve a ref (branch/tag/SHA) to a full 40-char commit SHA.

    Uses `git ls-remote`, which works against both remote URLs and local paths
    (the latter is how the offline fixture tests exercise this). Annotated tags
    are dereferenced to their peeled commit (the `^{}` line). A ref that is
    already a full SHA is validated and returned as-is.
    """
    if SHA_RE.match(ref.strip().lower()):
        return ref.strip().lower()

    proc = run_checked(["git", "ls-remote", upstream, ref], timeout=timeout)
    by_ref: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            by_ref[parts[1].strip()] = parts[0].strip()
    if not by_ref:
        raise VetError(f"ref {ref!r} did not resolve against {upstream}")

    # Prefer the peeled commit of an annotated tag.
    for refname, sha in by_ref.items():
        if refname.endswith("^{}"):
            return _validate_sha(sha)
    for candidate in (ref, f"refs/heads/{ref}", f"refs/tags/{ref}"):
        if candidate in by_ref:
            return _validate_sha(by_ref[candidate])
    # Single unambiguous match.
    return _validate_sha(next(iter(by_ref.values())))


# ── Hashing ──────────────────────────────────────────────────────────────────

def sha256_file(path: str | Path) -> str:
    """Return the bare lowercase sha256 hex of a file's bytes."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_skill_payload(repo_root: str | Path) -> dict[str, str]:
    """Hash the vetted skill payload at a checked-out repo.

    Covers agent_reach/skill/SKILL.md, SKILL_en.md, and every references/*.md.
    Keys are pin-relative names ("SKILL.md", "SKILL_en.md",
    "references/<name>.md"). Raises if the required SKILL.md is absent — a
    missing core skill file means the layout moved and must be re-checked.
    """
    skill_dir = Path(repo_root) / "agent_reach" / "skill"
    hashes: dict[str, str] = {}

    core = skill_dir / "SKILL.md"
    if not core.is_file():
        raise VetError(f"expected skill file not found: {core}")
    hashes["SKILL.md"] = sha256_file(core)

    en = skill_dir / "SKILL_en.md"
    if en.is_file():
        hashes["SKILL_en.md"] = sha256_file(en)

    refs_dir = skill_dir / "references"
    if refs_dir.is_dir():
        for ref_path in sorted(refs_dir.glob("*.md")):
            hashes[f"references/{ref_path.name}"] = sha256_file(ref_path)

    return dict(sorted(hashes.items()))


# ── Constant extraction ──────────────────────────────────────────────────────

def extract_str_constant(text: str, name: str) -> str:
    """Extract a `NAME = "value"` string constant from Python source text.

    Robust to single/double quotes and surrounding whitespace; used to pull
    OPENCLI_PACKAGE / OPENCLI_EXTENSION_ID / _RDT_GIT_SOURCE out of upstream so
    an upstream rename flows through rather than silently drifting.
    """
    pattern = re.compile(
        rf"^\s*{re.escape(name)}\s*=\s*[\"']([^\"']+)[\"']",
        re.MULTILINE,
    )
    match = pattern.search(text)
    if not match:
        raise VetError(f"could not find constant {name!r} in source")
    return match.group(1)


# ── Channel table ────────────────────────────────────────────────────────────

def build_channels(
    *,
    opencli_pkg: str,
    opencli_ext_id: str,
    rdt_git_source: str,
    npm_versions: dict[str, str],
    pypi_versions: dict[str, str],
) -> dict:
    """Assemble the pinned channel-backend table (pure — versions injected).

    Mirrors upstream agent_reach/cli.py CHANNEL_INSTALLERS at the vetted SHA.
    Channels with no installable external backend (local script, cookie-only,
    manual) are kept with an explicit note rather than dropped, so the runner's
    enable-channel validation recognises every real channel name.

    npm_versions maps an npm package name to its exact version; pypi_versions
    maps a PyPI package name to its exact version. The caller resolves these
    (network) before calling this function.
    """

    def npm(pkg: str) -> dict:
        version = npm_versions[pkg]
        return {"kind": "npm", "spec": f"{pkg}@{version}"}

    def pipx(pkg: str) -> dict:
        version = pypi_versions[pkg]
        return {"kind": "pipx", "spec": f"{pkg}=={version}"}

    opencli_backend = npm(opencli_pkg)
    # Note copy is em-dash-free: it may render verbatim in the dashboard.
    ext_note = {
        "note": (
            f"Chrome extension {opencli_ext_id} is manual; "
            "Chrome forbids auto-install."
        )
    }

    return {
        "twitter": {"backends": [pipx("twitter-cli")]},
        "xiaoyuzhou": {
            "backends": [
                {"note": "agent-reach installs a local transcribe script; no external package."}
            ]
        },
        "xiaohongshu": {
            "backends": [
                dict(opencli_backend),
                {"note": "Legacy xhs-cli is an optional fallback; upstream no longer auto-installs it."},
            ]
        },
        "reddit": {
            "backends": [
                dict(opencli_backend),
                {"kind": "pipx-git", "spec": rdt_git_source},
            ]
        },
        "facebook": {"backends": [dict(opencli_backend)]},
        "instagram": {"backends": [dict(opencli_backend)]},
        "bilibili": {"backends": [pipx("bilibili-cli")]},
        "opencli": {"backends": [dict(opencli_backend), ext_note]},
        "search": {"backends": [npm("mcporter")]},
        "xueqiu": {"backends": [{"note": "Cookie-only channel; no install step."}]},
        "linkedin": {"backends": [{"note": "Manual setup; no auto-install."}]},
    }


# ── Pin assembly + validation ────────────────────────────────────────────────

def build_pin(
    *,
    upstream: str,
    commit_sha: str,
    version_label: str,
    vetted_at: str,
    constraints_file: str,
    skill_hashes: dict[str, str],
    channels: dict,
) -> dict:
    """Assemble the pin dict in schema order (schema_version 1)."""
    return {
        "schema_version": 1,
        "upstream": upstream,
        "commit_sha": commit_sha,
        "version_label": version_label,
        "vetted_at": vetted_at,
        "constraints_file": constraints_file,
        "skill_hashes": dict(sorted(skill_hashes.items())),
        "channels": channels,
    }


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_backend(channel: str, backend: dict) -> None:
    if "note" in backend and "kind" not in backend:
        if not isinstance(backend["note"], str) or not backend["note"].strip():
            raise VetError(f"channel {channel!r}: note backend must be a non-empty string")
        return
    kind = backend.get("kind")
    spec = backend.get("spec")
    if kind not in VALID_BACKEND_KINDS:
        raise VetError(
            f"channel {channel!r}: backend kind must be one of {VALID_BACKEND_KINDS}, got {kind!r}"
        )
    if not isinstance(spec, str) or not spec.strip():
        raise VetError(f"channel {channel!r}: backend spec must be a non-empty string")
    if kind == "npm":
        # scoped (@scope/name@ver) or plain (name@ver): must carry a version.
        body = spec[1:] if spec.startswith("@") else spec
        if "@" not in body:
            raise VetError(f"channel {channel!r}: npm spec must pin a version (name@version): {spec!r}")
    elif kind == "pipx":
        if "==" not in spec:
            raise VetError(f"channel {channel!r}: pipx spec must pin an exact version (name==version): {spec!r}")
    elif kind == "pipx-git":
        if not spec.startswith("git+") or "@" not in spec:
            raise VetError(f"channel {channel!r}: pipx-git spec must be git+URL@SHA: {spec!r}")


def validate_pin(pin: dict) -> None:
    """Validate a pin dict against the normative schema. Raises VetError.

    Rejects: wrong schema_version, non-https upstream, a commit_sha that is not
    a full 40-char SHA (catches tag/branch-looking refs), bad date, missing or
    malformed skill hashes, and malformed channel backends.
    """
    if pin.get("schema_version") != 1:
        raise VetError(f"schema_version must be 1, got {pin.get('schema_version')!r}")

    upstream = pin.get("upstream")
    if not isinstance(upstream, str) or not upstream.startswith("https://"):
        raise VetError(f"upstream must be an https URL, got {upstream!r}")

    sha = pin.get("commit_sha")
    if not isinstance(sha, str) or not SHA_RE.match(sha):
        raise VetError(
            f"commit_sha must be a full 40-char lowercase SHA (never a tag/branch), got {sha!r}"
        )

    if not isinstance(pin.get("version_label"), str) or not pin["version_label"].strip():
        raise VetError("version_label must be a non-empty string")

    vetted_at = pin.get("vetted_at")
    if not isinstance(vetted_at, str) or not _DATE_RE.match(vetted_at):
        raise VetError(f"vetted_at must be YYYY-MM-DD, got {vetted_at!r}")

    if not isinstance(pin.get("constraints_file"), str) or not pin["constraints_file"].strip():
        raise VetError("constraints_file must be a non-empty string")

    skill_hashes = pin.get("skill_hashes")
    if not isinstance(skill_hashes, dict) or not skill_hashes:
        raise VetError("skill_hashes must be a non-empty object")
    if "SKILL.md" not in skill_hashes:
        raise VetError("skill_hashes must include SKILL.md")
    for name, value in skill_hashes.items():
        if not isinstance(value, str) or not SHA256_RE.match(value):
            raise VetError(f"skill hash for {name!r} must be a bare 64-char sha256 hex, got {value!r}")

    channels = pin.get("channels")
    if not isinstance(channels, dict) or not channels:
        raise VetError("channels must be a non-empty object")
    for channel, body in channels.items():
        backends = body.get("backends") if isinstance(body, dict) else None
        if not isinstance(backends, list) or not backends:
            raise VetError(f"channel {channel!r} must have a non-empty backends list")
        for backend in backends:
            if not isinstance(backend, dict):
                raise VetError(f"channel {channel!r}: each backend must be an object")
            _validate_backend(channel, backend)


# ── Diff vs current pin ──────────────────────────────────────────────────────

@dataclass
class DepDiff:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)

    def has_changes(self) -> bool:
        return bool(self.added or self.removed or self.changed)


@dataclass
class DiffReport:
    prior_sha: str
    head_sha: str
    changed_files: list[str] = field(default_factory=list)
    dep_diff: DepDiff = field(default_factory=DepDiff)
    risk_hits: dict[str, list[str]] = field(default_factory=dict)

    def has_changes(self) -> bool:
        return bool(self.changed_files)


def list_changed_files(repo_dir: str | Path, base_sha: str, head_sha: str, *, timeout: float = 30.0) -> list[str]:
    """Files changed between two SHAs (both must be present in repo_dir)."""
    proc = run_checked(
        ["git", "-C", str(repo_dir), "diff", "--name-only", base_sha, head_sha],
        timeout=timeout,
    )
    return sorted(f for f in proc.stdout.splitlines() if f.strip())


def read_file_at_sha(repo_dir: str | Path, sha: str, relpath: str, *, timeout: float = 30.0) -> str | None:
    """Return a file's text at a SHA, or None if it does not exist there."""
    proc = run_checked(
        ["git", "-C", str(repo_dir), "show", f"{sha}:{relpath}"],
        timeout=timeout,
        allow_fail=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def parse_pyproject_deps(text: str | None) -> list[str]:
    """Extract [project].dependencies (PEP 508 strings) from pyproject text."""
    if not text:
        return []
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise VetError(f"could not parse pyproject.toml: {exc}") from exc
    deps = data.get("project", {}).get("dependencies", [])
    return list(deps) if isinstance(deps, list) else []


def _dep_name(spec: str) -> str:
    return re.split(r"[<>=!~ \[]", spec.strip(), maxsplit=1)[0].lower()


def diff_deps(old_deps: list[str], new_deps: list[str]) -> DepDiff:
    """Compare two PEP 508 dependency lists by package name."""
    old = {_dep_name(d): d for d in old_deps}
    new = {_dep_name(d): d for d in new_deps}
    result = DepDiff()
    for name in sorted(set(old) | set(new)):
        if name not in old:
            result.added.append(new[name])
        elif name not in new:
            result.removed.append(old[name])
        elif old[name] != new[name]:
            result.changed.append(f"{old[name]} -> {new[name]}")
    return result


def scan_risk_patterns(repo_dir: str | Path, files: list[str]) -> dict[str, list[str]]:
    """Grep changed files (present in repo_dir) for subprocess/network patterns.

    Returns {relative_file: ["<lineno>: <pattern>: <line>", ...]}. Files absent
    from the working tree (deletions) or with non-scannable suffixes are skipped.
    """
    root = Path(repo_dir)
    hits: dict[str, list[str]] = {}
    for rel in files:
        path = root / rel
        if not path.is_file() or path.suffix.lower() not in _SCANNABLE_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        file_hits: list[str] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            for label, pattern in RISK_PATTERNS:
                if pattern.search(line):
                    file_hits.append(f"{lineno}: {label}: {line.strip()[:160]}")
        if file_hits:
            hits[rel] = file_hits
    return hits


def compute_diff(
    repo_dir: str | Path,
    prior_sha: str,
    head_sha: str,
    *,
    timeout: float = 30.0,
) -> DiffReport:
    """Full diff vs the current pin: changed files, dep changes, risk grep."""
    changed = list_changed_files(repo_dir, prior_sha, head_sha, timeout=timeout)
    old_pyproject = read_file_at_sha(repo_dir, prior_sha, "pyproject.toml", timeout=timeout)
    new_pyproject = read_file_at_sha(repo_dir, head_sha, "pyproject.toml", timeout=timeout)
    dep_diff = diff_deps(parse_pyproject_deps(old_pyproject), parse_pyproject_deps(new_pyproject))
    risk_hits = scan_risk_patterns(repo_dir, changed)
    return DiffReport(
        prior_sha=prior_sha,
        head_sha=head_sha,
        changed_files=changed,
        dep_diff=dep_diff,
        risk_hits=risk_hits,
    )
