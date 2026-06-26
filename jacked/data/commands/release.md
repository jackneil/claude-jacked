---
description: Use when ready to cut a release. Suggests the semver bump from commit history, gates on a local build + twine check + tests BEFORE tagging, pushes, creates the GitHub Release for PyPI publishing, then verifies the version is actually installable from the index.
---

You are the Release Manager. Execute the full release pipeline for claude-jacked.

## RELEASE PIPELINE

Follow these steps in order. Stop and report if any step fails.

### 1. PRE-FLIGHT CHECKS

Run these checks before anything else:

```bash
# Verify we're on master
git branch --show-current

# Verify working tree is clean (no uncommitted changes besides version bump)
git status --short

# Verify gh CLI is authenticated
gh auth status
```

**If not on master**: Stop. Ask the user if they want to merge first or release from this branch.
**If uncommitted changes exist**: List them. Ask the user whether to commit them as part of this release or stash them.
**If gh not authenticated**: Stop. Tell the user to run `gh auth login`.

### 2. DETERMINE VERSION

Read the current version from `jacked/__init__.py` (the `__version__` line).

**Suggest the bump from commit history first.** Find the last tag and scan the commits since it for Conventional Commit signals, then surface a recommendation (the user still picks):

```bash
# Last release tag (empty on a first release)
git describe --tags --abbrev=0 2>/dev/null
# Commits since that tag — look for feat: / fix: / BREAKING CHANGE / "!" markers
git log "$(git describe --tags --abbrev=0)"..HEAD --oneline
```

Map the strongest signal to a recommendation: a `BREAKING CHANGE` footer or `type!:` marker → **major**; any `feat:` → **minor**; only `fix:`/`chore:`/`docs:` etc. → **patch**. This is a non-binding suggestion to ground the choice in actual changes, not an auto-decision.

If `$ARGUMENTS` contains a version number (e.g. `0.14.0`), use that. Otherwise ask, leading with the suggestion:

```
Current version: X.Y.Z
Commits since vLAST: <N feat, M fix, K breaking>  →  suggested: <patch|minor|major>
What should the new version be?
- Patch (X.Y.Z+1) — bug fixes only
- Minor (X.Y+1.0) — new features, backward compatible
- Major (X+1.0.0) — breaking changes
- Custom — specify a version
```

### 3. BUMP VERSION

Edit `jacked/__init__.py` — change `__version__ = "..."` to the new version. Do NOT commit or tag yet — the build/test gate in step 4 must pass first.

### 4. BUILD + TEST GATE (before anything irreversible)

Tagging and the GitHub Release are immutable and the Release is what *triggers* the PyPI publish — so a broken build or failing test must be caught HERE, while everything is still local and reversible. Build the package and run the suite against the bumped tree before you create a tag:

```bash
# Clean build of wheel + sdist (uv build == python -m build for this repo)
rm -rf dist/
uv build
# Metadata/long-description sanity — same check PyPI applies on upload
uvx twine check dist/*
# Run the project's tests — publish.yml does NOT run them (see step 7)
uv run python -m pytest
```

- If `uv build` or `twine check` fails: fix the packaging issue (pyproject, missing files in the wheel, bad README) and re-run. Do NOT tag.
- If tests fail: stop and report. Do NOT tag a red tree.
- Only when build + `twine check` + tests are all green do you proceed to commit/tag.

### 5. COMMIT AND TAG

If a `CHANGELOG.md` exists, prepend a new section for this version (the delta since the last tag, grouped Features/Fixes/Other) BEFORE committing, so the tagged tree carries its own changelog. Then stage and commit the version bump along with any other approved changes:

```bash
git add jacked/__init__.py
# git add CHANGELOG.md   # if you updated it
# Include any other files the user approved in step 1
git commit -m "chore: bump version to X.Y.Z"
git tag vX.Y.Z
```

If there are substantive code changes to include (not just the version bump), ask the user for a commit message or use the format: `feat: vX.Y.Z — <summary of changes>`

### 6. PUSH

```bash
git push origin master --tags
```

If push fails due to upstream changes, stop and ask the user how to proceed (rebase, force push, etc.). Never force push without explicit user approval.

### 7. VERIFY CI (read this — it is NOT a green test gate here)

This repo's ONLY workflow is `publish.yml`, triggered `on: release: published`. It **builds and publishes — it does NOT run tests or lint.** There is no push/PR CI. So there is nothing to "wait for" on push, and "CI will validate the release" is false here. The local build + test gate in step 4 IS the validation gate — it must have passed before you got here.

```bash
# Confirm there is genuinely no push-triggered workflow (don't assume — verify)
gh run list --branch master --limit 3
```

If a push-triggered workflow DOES exist (someone added test CI), watch it and treat a failure as a hard stop:

```bash
gh run watch --exit-status            # only if a push run actually started
gh run view <run-id> --log-failed     # on failure — then stop, do NOT release
```

If none triggered (the expected state), proceed — step 4 already validated the build and tests locally.

### 8. CREATE GITHUB RELEASE

Optionally categorize the auto-generated notes by adding a `.github/release.yml` (GitHub groups merged PRs by label — Features/Fixes/etc.); without it, `--generate-notes` still lists PRs, contributors, and a full-changelog link.

```bash
gh release create vX.Y.Z \
  --title "vX.Y.Z" \
  --generate-notes \
  --latest
```

This triggers `.github/workflows/publish.yml` which:
1. Builds the package (wheel + sdist)
2. Publishes to PyPI via OIDC trusted publishing

### 9. VERIFY PYPI PUBLISH

After creating the release, monitor the publish workflow:

```bash
# Wait a moment for the workflow to trigger
gh run list --workflow=publish.yml --limit 3

# Watch the publish run
gh run watch --exit-status
```

If the publish workflow fails:
- Show logs: `gh run view <run-id> --log-failed`
- **The tag and GitHub Release now exist but the version is NOT on PyPI — that is the failure mode this command works to avoid.** Tell the user to fix the cause and either re-run the workflow from the Actions tab (preferred — keeps the tag) or, if the artifact itself is bad, delete the Release + tag and re-cut after a fix. Never just leave a `--latest` Release pointing at a version absent from PyPI.

### 10. VERIFY THE ARTIFACT IS INSTALLABLE

A green workflow means "upload returned OK," not "resolvable from the index" — CDN propagation or a metadata issue can lag. Confirm the version is actually live before declaring success. Derive the project name from `pyproject.toml` (`[project] name`) rather than hardcoding it:

```bash
NAME=$(grep -m1 '^name' pyproject.toml | sed -E 's/.*"([^"]+)".*/\1/')   # claude-jacked
# Poll the index until the new version appears in releases
curl -fsSL "https://pypi.org/pypi/${NAME}/json" | grep -q '"X.Y.Z"'
# Optional stronger proof — clean install in a throwaway env:
uv tool install "${NAME}@X.Y.Z" --force
```

> Naming caveat to confirm, not assume: `pyproject.toml` declares `name = "claude-jacked"`, but `publish.yml`'s environment url is `https://pypi.org/p/jacked`. If install verification 404s, the Trusted Publisher / project name may be registered under a different name than the wheel — surface this to the user instead of guessing.

Once the version is confirmed resolvable, confirm (use the derived `$NAME`, do not hardcode):

```
Release vX.Y.Z complete and installable!
- GitHub: https://github.com/jackneil/claude-jacked/releases/tag/vX.Y.Z
- PyPI:   https://pypi.org/project/<NAME>/X.Y.Z/
- Install: uv tool install <NAME>@X.Y.Z
```

## HARD RULES

- Never force push without explicit user approval
- The tag and GitHub Release must NOT outlive a failed PyPI upload — gate on a local build + `twine check` + tests (step 4) BEFORE tagging; this repo's CI does not test for you
- Never create a release if a real test gate is red — locally in step 4, or a push CI run if one exists
- Never skip the version bump — PyPI rejects duplicate versions
- Always verify the publish workflow succeeds AND the version is installable from the index before declaring done
- Derive the project name from pyproject; don't hardcode PyPI URLs
- If anything fails, stop and report — do not retry destructively
