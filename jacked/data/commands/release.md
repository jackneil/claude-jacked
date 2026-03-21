---
description: "Release workflow — bump version, commit, push, verify CI, create GitHub Release for PyPI publishing"
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

If `$ARGUMENTS` contains a version number (e.g. `0.14.0`), use that.
Otherwise, ask the user:

```
Current version: X.Y.Z
What should the new version be?
- Patch (X.Y.Z+1) — bug fixes only
- Minor (X.Y+1.0) — new features, backward compatible
- Major (X+1.0.0) — breaking changes
- Custom — specify a version
```

### 3. BUMP VERSION

Edit `jacked/__init__.py` — change `__version__ = "..."` to the new version.

### 4. COMMIT AND TAG

Stage and commit the version bump along with any other staged changes:

```bash
git add jacked/__init__.py
# Include any other files the user approved in step 1
git commit -m "chore: bump version to X.Y.Z"
git tag vX.Y.Z
```

If there are substantive code changes to include (not just the version bump), ask the user for a commit message or use the format: `feat: vX.Y.Z — <summary of changes>`

### 5. PUSH

```bash
git push origin master --tags
```

If push fails due to upstream changes, stop and ask the user how to proceed (rebase, force push, etc.). Never force push without explicit user approval.

### 6. VERIFY CI

Wait for CI to complete on the pushed commit:

```bash
# Watch the CI run — check every 15 seconds
gh run list --branch master --limit 3
gh run watch --exit-status
```

If CI fails:
- Show the failure details: `gh run view <run-id> --log-failed`
- Stop and report. Do NOT create a release with failing CI.

If no CI workflow triggers on push (the publish workflow only triggers on release creation, which is expected), check for any other workflows that run on push. If none exist, proceed — CI will run as part of the release.

### 7. CREATE GITHUB RELEASE

```bash
gh release create vX.Y.Z \
  --title "vX.Y.Z" \
  --generate-notes \
  --latest
```

This triggers `.github/workflows/publish.yml` which:
1. Builds the package (wheel + sdist)
2. Publishes to PyPI via OIDC trusted publishing

### 8. VERIFY PYPI PUBLISH

After creating the release, monitor the publish workflow:

```bash
# Wait a moment for the workflow to trigger
gh run list --workflow=publish.yml --limit 3

# Watch the publish run
gh run watch --exit-status
```

If the publish workflow fails:
- Show logs: `gh run view <run-id> --log-failed`
- The release exists but PyPI publish failed. Tell the user they can re-run the workflow from the GitHub Actions tab or delete the release and retry.

If it succeeds, confirm:

```
Release vX.Y.Z complete!
- GitHub: https://github.com/jackneill/claude-jacked/releases/tag/vX.Y.Z
- PyPI: https://pypi.org/project/claude-jacked/X.Y.Z/
- Install: uv tool install claude-jacked@X.Y.Z
```

## HARD RULES

- Never force push without explicit user approval
- Never create a release if CI is failing on the commit
- Never skip the version bump — PyPI rejects duplicate versions
- Always verify the publish workflow succeeds before declaring done
- If anything fails, stop and report — do not retry destructively
