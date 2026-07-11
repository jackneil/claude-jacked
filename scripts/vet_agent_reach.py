#!/usr/bin/env python3
# scripts/vet_agent_reach.py
"""Vet an Agent-Reach ref and emit jacked's vendored trust anchor.

Repo-side tool (NOT shipped in the wheel). Given a ref (default upstream main),
it resolves the ref to an immutable commit SHA, fetches the tree at that SHA,
diffs it against the current pin, compiles a fully-pinned constraints set,
audits that set with pip-audit, hashes the skill payload, resolves the exact
channel-backend versions, and writes:

  jacked/data/integrations/agent-reach.json          (the pin)
  jacked/data/integrations/agent-reach-constraints.txt (locked deps)
  <scratch>/agent-reach-vetting-report.md            (human review report)

The pin + constraints are the files a maintainer commits in a bump PR after
reading the report. Nothing here installs or runs Agent-Reach.

Usage:
    uv run python scripts/vet_agent_reach.py [--ref main] [--work-dir DIR]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

# Allow running both as `python scripts/vet_agent_reach.py` and as a module.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _vet_helpers import (  # noqa: E402
    VetError,
    build_channels,
    build_pin,
    compute_diff,
    extract_str_constant,
    hash_skill_payload,
    resolve_ref_to_sha,
    run_checked,
    validate_pin,
)

DEFAULT_UPSTREAM = "https://github.com/Panniantong/Agent-Reach"
PIN_FILENAME = "agent-reach.json"
CONSTRAINTS_FILENAME = "agent-reach-constraints.txt"
REPORT_FILENAME = "agent-reach-vetting-report.md"

# Exact npm / PyPI packages the channel backends pull, discovered by reading
# upstream cli.py. Names resolve to exact versions at vet time; upstream renames
# of OPENCLI_PACKAGE flow through the extracted constant.
NPM_PACKAGES = ("mcporter",)  # @jackwener/opencli name comes from the constant
PYPI_PACKAGES = ("twitter-cli", "bilibili-cli")

NET_TIMEOUT = 30.0
COMPILE_TIMEOUT = 300.0
AUDIT_TIMEOUT = 300.0
CLONE_TIMEOUT = 180.0


# ── Network resolvers (monkeypatched out in offline tests) ───────────────────

def resolve_npm_version(pkg: str, *, timeout: float = NET_TIMEOUT) -> str:
    """Exact current version of an npm package via `npm view <pkg> version`."""
    proc = run_checked(["npm", "view", pkg, "version"], timeout=timeout)
    version = proc.stdout.strip().splitlines()[-1].strip() if proc.stdout.strip() else ""
    if not version:
        raise VetError(f"npm view returned no version for {pkg!r}")
    return version


def resolve_pypi_version(pkg: str, *, timeout: float = NET_TIMEOUT) -> str:
    """Exact current version of a PyPI package via the JSON API."""
    url = f"https://pypi.org/pypi/{pkg}/json"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (fixed https host)
            payload = json.load(resp)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise VetError(f"PyPI lookup failed for {pkg!r}: {exc}") from exc
    version = payload.get("info", {}).get("version")
    if not version:
        raise VetError(f"PyPI returned no version for {pkg!r}")
    return version


# ── Git ──────────────────────────────────────────────────────────────────────

def clone_at_sha(upstream: str, sha: str, dest: Path, *, timeout: float = CLONE_TIMEOUT) -> Path:
    """Shallow-fetch exactly one commit SHA and check it out into dest."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    run_checked(["git", "init", "-q"], cwd=dest, timeout=timeout)
    run_checked(["git", "remote", "add", "origin", upstream], cwd=dest, timeout=timeout)
    run_checked(["git", "fetch", "--depth", "1", "origin", sha], cwd=dest, timeout=timeout)
    run_checked(["git", "checkout", "-q", "FETCH_HEAD"], cwd=dest, timeout=timeout)
    return dest


def try_fetch_sha(repo_dir: Path, sha: str, *, timeout: float = CLONE_TIMEOUT) -> bool:
    """Best-effort shallow fetch of a prior SHA for diffing. Never raises."""
    try:
        run_checked(["git", "fetch", "--depth", "1", "origin", sha], cwd=repo_dir, timeout=timeout)
        return True
    except VetError:
        return False


# ── Constraints + audit ──────────────────────────────────────────────────────

def compile_constraints(repo_dir: Path, out_name: str, *, exclude_newer: str | None, timeout: float = COMPILE_TIMEOUT) -> str:
    """`uv pip compile --universal` the upstream pyproject into a locked set.

    Compiles to a stable RELATIVE filename inside the clone (cwd=repo_dir) so the
    header uv embeds is deterministic across machines (no absolute scratch path).
    """
    cmd = ["uv", "pip", "compile", "pyproject.toml", "--universal", "-o", out_name]
    if exclude_newer:
        cmd += ["--exclude-newer", exclude_newer]
    run_checked(cmd, cwd=repo_dir, timeout=timeout)
    return (repo_dir / out_name).read_text(encoding="utf-8")


def audit_constraints(constraints_path: Path, *, timeout: float = AUDIT_TIMEOUT) -> tuple[bool, str]:
    """Run pip-audit over the locked set. Returns (clean, verbatim_output).

    Never fails the run on findings — the human review gate decides. The
    verbatim output is captured for the report either way.
    """
    proc = run_checked(
        ["uvx", "pip-audit", "-r", str(constraints_path)],
        timeout=timeout,
        allow_fail=True,
    )
    output = (proc.stdout or "") + (proc.stderr or "")
    clean = proc.returncode == 0 and "no known vulnerabilities" in output.lower()
    return clean, output.strip()


# ── Pin assembly ─────────────────────────────────────────────────────────────

def _read_required(repo_dir: Path, relpath: str) -> str:
    """Read an expected upstream file, or fail loud on layout drift.

    A missing file here means upstream moved things relative to the vetted
    layout (Verified Upstream Facts) — stop and force a human re-check of the
    channel model rather than emitting a silently-wrong pin.
    """
    path = repo_dir / relpath
    if not path.is_file():
        raise VetError(
            f"expected upstream file not found: {relpath}. "
            "Upstream layout differs from the vetted facts; re-check the channel model before pinning."
        )
    return path.read_text(encoding="utf-8")


def read_version_label(repo_dir: Path) -> str:
    import tomllib

    data = tomllib.loads(_read_required(repo_dir, "pyproject.toml"))
    version = data.get("project", {}).get("version")
    if not version:
        raise VetError("upstream pyproject.toml has no [project].version")
    return str(version)


def resolve_channels(repo_dir: Path) -> dict:
    """Extract upstream constants, resolve exact versions, build the table."""
    opencli_src = _read_required(repo_dir, "agent_reach/backends/opencli.py")
    cli_src = _read_required(repo_dir, "agent_reach/cli.py")

    opencli_pkg = extract_str_constant(opencli_src, "OPENCLI_PACKAGE")
    opencli_ext_id = extract_str_constant(opencli_src, "OPENCLI_EXTENSION_ID")
    rdt_git_source = extract_str_constant(cli_src, "_RDT_GIT_SOURCE")

    npm_versions = {opencli_pkg: resolve_npm_version(opencli_pkg)}
    for pkg in NPM_PACKAGES:
        npm_versions[pkg] = resolve_npm_version(pkg)
    pypi_versions = {pkg: resolve_pypi_version(pkg) for pkg in PYPI_PACKAGES}

    return build_channels(
        opencli_pkg=opencli_pkg,
        opencli_ext_id=opencli_ext_id,
        rdt_git_source=rdt_git_source,
        npm_versions=npm_versions,
        pypi_versions=pypi_versions,
    )


def write_pin(pin: dict, out_dir: Path) -> Path:
    path = out_dir / PIN_FILENAME
    path.write_text(json.dumps(pin, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def write_constraints(compiled: str, commit_sha: str, vetted_at: str, out_dir: Path) -> Path:
    path = out_dir / CONSTRAINTS_FILENAME
    header = (
        "# Agent-Reach vendored constraints. DO NOT EDIT BY HAND.\n"
        f"# Regenerate: uv run python scripts/vet_agent_reach.py\n"
        f"# Pinned commit: {commit_sha}\n"
        f"# Vetted: {vetted_at}\n\n"
    )
    path.write_text(header + compiled, encoding="utf-8")
    return path


# ── Report ───────────────────────────────────────────────────────────────────

def render_report(pin, diff, audit_clean, audit_output, prior_sha, prior_pin_note) -> str:
    lines: list[str] = []
    add = lines.append
    add(f"# Agent-Reach vetting report — {pin['vetted_at']}\n")
    add(f"- Upstream: {pin['upstream']}")
    add(f"- Ref resolved to SHA: `{pin['commit_sha']}`")
    add(f"- Version label (upstream pyproject): `{pin['version_label']}`")
    add(f"- Constraints file: `{pin['constraints_file']}`")
    add(f"- pip-audit verdict: {'CLEAN' if audit_clean else 'REVIEW REQUIRED — see output below'}\n")

    add("## Diff vs current pin\n")
    if prior_pin_note:
        add(prior_pin_note + "\n")
    elif diff is not None:
        add(f"Prior pinned SHA: `{prior_sha}`\n")
        add(f"Changed files ({len(diff.changed_files)}):")
        for f in diff.changed_files:
            add(f"  - {f}")
        add("")
        dd = diff.dep_diff
        add("Dependency changes:")
        if not dd.has_changes():
            add("  (none)")
        for label, items in (("added", dd.added), ("removed", dd.removed), ("changed", dd.changed)):
            for item in items:
                add(f"  - {label}: {item}")
        add("")
        add("Risk-pattern hits in changed files (subprocess/network heuristics):")
        if not diff.risk_hits:
            add("  (none)")
        for f, hits in diff.risk_hits.items():
            add(f"  {f}:")
            for h in hits:
                add(f"    {h}")
        add("")

    add("## Skill payload hashes\n")
    for name, h in pin["skill_hashes"].items():
        add(f"  - {name}: {h}")
    add("")

    add("## Channel backend version table\n")
    for channel, body in pin["channels"].items():
        specs = []
        for backend in body["backends"]:
            if "spec" in backend:
                specs.append(f"{backend['kind']}:{backend['spec']}")
            elif "note" in backend:
                specs.append(f"note:{backend['note']}")
        add(f"  - {channel}: " + "; ".join(specs))
    add("")

    add("## pip-audit output (verbatim)\n")
    add("```")
    add(audit_output or "(no output)")
    add("```")
    return "\n".join(lines) + "\n"


# ── Orchestration ────────────────────────────────────────────────────────────

def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def vet(args: argparse.Namespace) -> int:
    upstream = args.upstream
    out_dir = Path(args.output_dir) if args.output_dir else repo_root() / "jacked" / "data" / "integrations"
    out_dir.mkdir(parents=True, exist_ok=True)

    work_dir = Path(args.work_dir) if args.work_dir else Path("/tmp") / "vet-agent-reach"
    clone_dir = work_dir / "clone"
    report_dir = Path(args.report_dir) if args.report_dir else work_dir
    report_dir.mkdir(parents=True, exist_ok=True)
    vetted_at = args.vetted_at or date.today().isoformat()

    print(f"[1/7] Resolving ref {args.ref!r} against {upstream} ...")
    sha = resolve_ref_to_sha(upstream, args.ref, timeout=NET_TIMEOUT)
    print(f"       -> {sha}")

    print(f"[2/7] Fetching tree at {sha[:12]} into {clone_dir} ...")
    clone_at_sha(upstream, sha, clone_dir)
    version_label = read_version_label(clone_dir)
    print(f"       upstream version label: {version_label}")

    print("[3/7] Diffing against current pin ...")
    diff = None
    prior_sha = None
    prior_pin_note = None
    pin_path = out_dir / PIN_FILENAME
    if pin_path.is_file():
        prior_pin = json.loads(pin_path.read_text(encoding="utf-8"))
        prior_sha = prior_pin.get("commit_sha")
        if prior_sha == sha:
            prior_pin_note = f"No change: current pin already at `{sha}`."
        elif prior_sha and try_fetch_sha(clone_dir, prior_sha):
            diff = compute_diff(clone_dir, prior_sha, sha)
            print(f"       {len(diff.changed_files)} files changed since {prior_sha[:12]}")
        else:
            prior_pin_note = (
                f"Prior pin `{prior_sha}` could not be fetched for a diff "
                "(unreachable SHA). Review the full tree manually."
            )
    else:
        prior_pin_note = "No current pin — this is the initial vetting."
    if prior_pin_note:
        print(f"       {prior_pin_note}")

    print("[4/7] Compiling fully-pinned constraints (uv pip compile --universal) ...")
    compiled = compile_constraints(clone_dir, CONSTRAINTS_FILENAME, exclude_newer=args.exclude_newer)
    compiled_path = clone_dir / CONSTRAINTS_FILENAME

    print("[5/7] Auditing locked set (uvx pip-audit) ...")
    audit_clean, audit_output = audit_constraints(compiled_path)
    print(f"       verdict: {'CLEAN' if audit_clean else 'REVIEW REQUIRED'}")

    print("[6/7] Hashing skill payload + resolving channel versions ...")
    skill_hashes = hash_skill_payload(clone_dir)
    channels = resolve_channels(clone_dir)

    print("[7/7] Building + validating pin, writing files ...")
    pin = build_pin(
        upstream=upstream,
        commit_sha=sha,
        version_label=version_label,
        vetted_at=vetted_at,
        constraints_file=CONSTRAINTS_FILENAME,
        skill_hashes=skill_hashes,
        channels=channels,
    )
    validate_pin(pin)
    pin_out = write_pin(pin, out_dir)
    constraints_out = write_constraints(compiled, sha, vetted_at, out_dir)

    report = render_report(pin, diff, audit_clean, audit_output, prior_sha, prior_pin_note)
    report_path = report_dir / REPORT_FILENAME
    report_path.write_text(report, encoding="utf-8")

    print("\n=== DONE ===")
    print(f"pin:         {pin_out}")
    print(f"constraints: {constraints_out}")
    print(f"report:      {report_path}")
    print(f"SHA:         {sha}")
    print(f"version:     {version_label}")
    print(f"pip-audit:   {'CLEAN' if audit_clean else 'REVIEW REQUIRED'}")
    print("\nChannel table:")
    for channel, body in channels.items():
        specs = "; ".join(
            b.get("spec", f"note:{b.get('note', '')}") for b in body["backends"]
        )
        print(f"  {channel}: {specs}")
    if not audit_clean:
        print("\n[!] pip-audit flagged findings — see report. Files still emitted for the review gate.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Vet an Agent-Reach ref and emit jacked's vendored pin.")
    parser.add_argument("--ref", default="main", help="Upstream ref (branch/tag/SHA) to vet. Default: main.")
    parser.add_argument("--upstream", default=DEFAULT_UPSTREAM, help="Upstream repo URL.")
    parser.add_argument("--output-dir", default=None, help="Where to write the pin + constraints (default: jacked/data/integrations).")
    parser.add_argument("--work-dir", default=None, help="Scratch dir for the clone + intermediate files.")
    parser.add_argument("--report-dir", default=None, help="Where to write the vetting report (default: work dir).")
    parser.add_argument("--vetted-at", default=None, help="Override the vetted date (YYYY-MM-DD). Default: today.")
    parser.add_argument(
        "--exclude-newer",
        default=None,
        help="Optional uv --exclude-newer date (e.g. 2026-06-01) to refuse deps newer than the given day, "
             "matching jacked's smash-and-grab defense. Default: off.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return vet(args)
    except VetError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
