---
description: Use when ready to cut a release. Detects how THIS repo actually ships (PyPI, npm, changesets, CalVer, Cargo, Go tag, or PR-to-main deploy), suggests the semver bump, gates on the repo's OWN build+test before anything irreversible, then publishes/deploys the way that repo really ships and verifies it landed.
---

> **Note:** If `.claude/commands/release.md` exists in the current repo, that version has pre-filled repo config from `/jacked-setup release` — use it instead of this global file. If it doesn't exist, continue here.

You are the Release Manager. Cut a release for **this** repo — however it actually ships. Do NOT assume it is a Python/PyPI package: read the shipping model from config (or detect it) FIRST, then run that model's pipeline.

## Config Override

If invoked via a local config wrapper (you see a `## Repo Config` section earlier in the prompt), it is **authoritative** — use it, do not re-detect:
- **Base Branch** → the branch to release from / push to (not a hardcoded `master`).
- **Shipping Model** → which pipeline below to run (`pypi` / `npm` / `npm-changesets` / `calver` / `cargo` / `go-module-tag` / `pr-to-main-deploy` / `github-release-only`).
- **Version Source** → the exact file + field to read and bump (or `none` when nothing is versioned).
- **Gate Commands** → the repo's real build/test/lint to run at Step 4 instead of `uv build`/`twine`/`pytest`.
- **Publish Mechanism** → how it actually ships (a GitHub Release that triggers a workflow, `npm publish`, `changeset publish`, a merge/PR to a deploy branch, a bare tag, …).
- **Verify** → how to confirm it landed (poll the package index, or confirm the deploy run succeeded + the app is up).

If no `## Repo Config` is present, auto-detect the model in the next section.

**SAFETY — this command does irreversible things (publishes, tags, deploys):**
- The config/detected model decides the publish **target**. If the model or target is ambiguous, or you detect more than one plausible target, **STOP and ask** — never guess where to publish or which branch to push.
- **Never publish to a package index for a repo that has no package** (a private app / `pr-to-main-deploy` repo). Never invent a PyPI/npm step that the repo's own CI doesn't already do.
- Everything reversible (version bump, gate) happens BEFORE anything irreversible (tag, publish, merge-to-deploy-branch). Never force-push without explicit approval.

## Auto-detect the shipping model (when no Repo Config)

Detect conservatively — read, don't assume:

```bash
ls pyproject.toml setup.py package.json Cargo.toml go.mod 2>/dev/null   # ecosystem
ls -d .changeset 2>/dev/null                                            # npm changesets
ls .github/workflows/ 2>/dev/null
# What does CI actually DO on release/push/tag? (the source of truth for how it ships)
grep -rlE 'twine|pypi|gh-action-pypi|npm publish|changeset|cargo publish|railway|vercel|fly\.io|flyctl|deploy' .github/workflows/ 2>/dev/null
grep -rlE 'on:|release:|push:|tags:|workflow_dispatch:' .github/workflows/ 2>/dev/null
git remote -v 2>/dev/null; git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||'
```

Map signals → model (priority order; when two genuinely fit, **ask**):
- `.changeset/` + `package.json` → **npm-changesets** (`changeset version` bumps, `changeset publish` ships).
- `package.json` + an `npm publish` / `publishConfig` in a workflow → **npm** — but if versions are date-shaped (`2026.318.0`) or a workflow derives the version from the date → **calver**.
- `pyproject.toml`/`setup.py` + a workflow that publishes to PyPI (twine / OIDC trusted publisher) → **pypi**.
- `Cargo.toml` + `cargo publish` → **cargo**.
- `go.mod`, no publish step → **go-module-tag** (the tag itself IS the release; the module proxy serves it).
- A workflow that **deploys** on push/merge to the base branch (railway/vercel/fly/a deploy step) with NO package publish → **pr-to-main-deploy**: "shipping" = getting the change onto the deploy branch; there is no version bump or index publish, and cutting a package would be wrong.
- None of the above, but you still want a GitHub Release for notes → **github-release-only** (tag + Release, no publish).

State the detected model and the evidence you based it on. **If unsure, ask before touching anything.**

## RELEASE PIPELINE

Run in order. Stop and report if any step fails.

### 1. PRE-FLIGHT

```bash
BASE="${BASE_BRANCH:-$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||' || echo main)}"
git branch --show-current      # should be the base branch (or the branch you're releasing)
git status --short             # working tree state
gh auth status                 # GitHub auth (needed for Releases / PR merges)
```
- **Not on the base branch** → stop; ask whether to merge first or release from this branch.
- **Uncommitted changes** → list them; ask whether to include them in the release or stash.
- **gh not authenticated** → stop; tell the user to run `gh auth login`.

### 2. DETERMINE VERSION

**Skip this step entirely for `calver` (the version is computed, usually date-based — read how from the workflow) and `pr-to-main-deploy` / any `none` Version Source (nothing is versioned — the merge is the ship).**

Otherwise read the current version from the **Version Source**:
- Config declares it → use that file+field.
- Else detect: `pyproject.toml` `[project].version` if static; if `dynamic = ["version"]`, follow `[tool.hatch.version].path` (or `grep -rl __version__`) to the real file; `package.json` `version`; `Cargo.toml` `[package].version`. In a monorepo, the versioned artifact may live in a workspace member, not the root — read the member that actually ships.

**Suggest the bump from commit history** (the user still picks):
```bash
git describe --tags --abbrev=0 2>/dev/null
git log "$(git describe --tags --abbrev=0 2>/dev/null)"..HEAD --oneline 2>/dev/null
```
`BREAKING CHANGE`/`type!:` → **major**; any `feat:` → **minor**; only `fix:`/`chore:`/`docs:` → **patch**. If `$ARGUMENTS` has a version, use it. Otherwise ask, leading with the suggestion (Patch / Minor / Major / Custom).

### 3. BUMP VERSION

Edit the Version Source to the new version (`__version__`, `[project].version`, `package.json` "version", `Cargo.toml`, etc.). For `npm-changesets`, run the changeset flow instead (`pnpm changeset` → `changeset version`). Do NOT commit or tag yet — the gate (Step 4) must pass first. Skip for `calver`/`none`.

### 4. GATE — before anything irreversible

The tag/Release/publish/deploy are what ship the code, and many repos' CI does NOT run tests on release — so catch a broken build or red test HERE, while everything is still local and reversible. Run the repo's **own** gate:
- **Config `Gate Commands`** → run exactly those, in order.
- **Else per model** (run what the repo actually uses — read `package.json` scripts / the CI workflow, don't assume):
  - `pypi`: `rm -rf dist/ && uv build && uvx twine check dist/* && uv run python -m pytest`
  - `npm` / `npm-changesets` / `calver`: the repo's real chain, e.g. `pnpm -r typecheck && pnpm -r test && pnpm -r build` (or `npm ci && npm test && npm run build`; turbo repos: `turbo run typecheck test build`).
  - `cargo`: `cargo build --release && cargo test`.
  - `go-module-tag`: `go build ./... && go test ./...`.
  - `pr-to-main-deploy`: run the repo's CI gate locally (the lint/typecheck/test/build the deploy workflow runs) so the merge doesn't break the deploy.

Only when the gate is fully green do you proceed. If it fails, fix the cause and re-run — do NOT tag/publish/merge a red tree.

### 5. CHANGELOG + COMMIT + TAG

If a `CHANGELOG.md` exists (or the README carries a "Version History" section), prepend the new version's delta (grouped Features/Fixes/Other) before committing, so the tagged tree carries its own changelog. Then:
```bash
git add <version file> <changelog>   # + any other approved changes
git commit -m "chore: release vX.Y.Z"   # or feat/fix: vX.Y.Z — <summary> for substantive changes
git tag vX.Y.Z                          # SKIP the tag for pr-to-main-deploy (the merge is the ship; tag only if the repo tags)
```

### 6. PUSH / SHIP

- **pypi / npm / cargo / go-module-tag / github-release-only:** push the base branch + tag.
  ```bash
  git push origin "$BASE" && git push origin vX.Y.Z
  ```
- **pr-to-main-deploy:** getting the change onto the deploy branch IS the release. If you're on a feature branch, open a PR to the deploy branch and (once its CI is green) merge with a true merge commit (`gh pr merge --merge`, never `--squash`/`--rebase`/`--admin`); if you're already on the deploy branch with approval, push it. The merge/push triggers the deploy — there is no package to publish.

If push fails on upstream changes, stop and ask (never force-push without explicit approval).

### 7. PUBLISH (per model)

- **pypi:** create the GitHub Release — this triggers the publish workflow (`gh release create vX.Y.Z --title "vX.Y.Z" --generate-notes --latest`). Then watch it: find the run for THIS tag (`gh run list --workflow=<publish>.yml` and select the one whose `headBranch` == `vX.Y.Z` — do not grab a stale prior run) and `gh run watch <id> --exit-status`.
- **npm / calver:** if a workflow publishes on Release/tag, cut the Release and watch that run as above. If publishing is manual, run the repo's publish (`npm publish` / `pnpm -r publish`) with the right registry/OTP.
- **npm-changesets:** `pnpm changeset publish` (or let the Changesets Action open/merge the version PR, then publish) — follow the repo's setup; watch the action.
- **cargo:** `cargo publish` (per crate, in dependency order for a workspace).
- **go-module-tag:** nothing to publish — the pushed tag is the release. Optionally `gh release create` for notes.
- **github-release-only:** `gh release create vX.Y.Z --generate-notes --latest`; no index publish.
- **pr-to-main-deploy:** nothing to publish. **Publishing here = watching the DEPLOY**: find the deploy workflow run triggered by the merge and `gh run watch <id> --exit-status`; a red deploy is a failed release.

If a publish/deploy run fails: `gh run view <id> --log-failed`, report it, and **never leave a `--latest` Release (or a "shipped" claim) pointing at a version/deploy that isn't actually live** — re-run from Actions (keeps the tag) or delete the Release+tag and re-cut after a fix.

### 8. VERIFY IT LANDED

A green workflow means "the job succeeded," not "it's live." Confirm reality:
- **pypi:** poll the index until the version resolves, then prove installability (derive the name from `pyproject.toml`, don't hardcode):
  ```bash
  NAME=$(grep -m1 '^name' pyproject.toml | sed -E 's/.*"([^"]+)".*/\1/')
  curl -fsSL "https://pypi.org/pypi/${NAME}/json" | grep -q '"X.Y.Z"'   # JSON API
  # and the /simple/ index (what installers resolve) — it can lag the JSON API by a minute
  # then, from a NEUTRAL dir (outside the repo, to avoid a pinned exclude-newer): uv tool install "${NAME}@X.Y.Z" --force --refresh
  ```
- **npm / calver:** `npm view <pkg>@X.Y.Z version` until it resolves (and `npm install` in a throwaway dir for the strong proof).
- **cargo:** `cargo search <crate>` / crates.io shows the version.
- **go-module-tag:** `go list -m <module>@vX.Y.Z` resolves via the proxy.
- **pr-to-main-deploy:** the deploy run is green AND the app is actually up — `curl -fsSItimeout` the production/preview URL (from config or the deploy output) for a 2xx, and sanity-check a health route if one exists. A green deploy job with a 502 site is a failed release.

Then report the concrete artifact: the Release URL, the index/registry URL + install line, or the live app URL — whichever the model produced.

### 9. RECORD (memory vault, guarded)

After the release is verified live, if the memory vault is enabled (`jacked memory status --quiet` exits 0; skip silently if it exits nonzero), record a progress note so the shipped version and its highlights are searchable later:

```bash
jacked memory add --type progress --title "Released vX.Y.Z" --body "<the headline changes + the artifact URL>"
```

One note per release, high-signal. If the vault is off, do nothing.

## HARD RULES

- Read the shipping model from config or detect it FIRST; when the model or publish target is ambiguous, STOP and ask. Never guess a publish target or push to the wrong branch.
- Never publish to a package index for a repo that has no package (pr-to-main-deploy / private app). Never invent a publish step the repo's own CI doesn't do.
- Reversible before irreversible: gate (build + tests, the repo's OWN) must be green BEFORE any tag/publish/merge-to-deploy. Never tag/ship a red tree.
- Never force-push without explicit approval. Never `--squash`/`--rebase`/`--admin`-merge a release PR — use a true merge commit.
- Never skip the version bump for a versioned model (indexes reject duplicate versions) — and never bump for a model that doesn't version (calver/pr-to-main-deploy).
- A tag/Release must NOT outlive a failed publish/deploy — verify the artifact is actually live/installable (or the deploy is actually up) before declaring done.
- If anything fails, stop and report — do not retry destructively.
