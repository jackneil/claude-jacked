---
description: Use after a PR is approved and CI passes. Merges, waits for deploy, runs canary checks, and offers revert on failure.
---

You are running the land-and-deploy pipeline. This picks up where `/commit-push-pr` leaves off — merging the PR, waiting for CI and deployment, then verifying the production site is healthy.

## Arguments

`$ARGUMENTS` controls behavior:
- Empty → auto-detect current branch's PR
- `<PR number>` → operate on a specific PR
- `--skip-canary` → merge and wait for deploy but skip canary monitoring
- `--revert` → revert the most recent deploy (if something went wrong)

## Step 1: Identify the PR

```bash
# Get current branch
git branch --show-current
```

```bash
# Find PR for current branch
gh pr view --json number,title,state,mergeable,statusCheckRollup,headRefName,baseRefName,url 2>/dev/null
```

If no PR exists for the current branch:
```
No PR found for branch [branch-name].
Run /pr to create one first, or specify a PR number: /land-and-deploy 123
```
Stop.

If the PR is already merged: skip to Step 4 (deploy detection).

## Step 2: Pre-Merge Readiness Check

Verify the PR is ready to merge:

```bash
# Check CI status
gh pr checks --json name,state,conclusion 2>/dev/null
```

```bash
# Check review status
gh pr view --json reviewDecision,reviews 2>/dev/null
```

Report readiness:
```
PR #N: [title]
- CI checks: [all passing / N failing]
- Reviews: [approved / changes requested / pending]
- Mergeable: [yes / no — reason]
```

**If CI is failing:** "CI checks are failing. Fix the failures before merging." List the failing checks. Stop.

**If not approved:** "PR needs approval. Request a review or merge manually if you have permission." Stop.

**If ready:** proceed to merge.

## Step 3: Merge the PR

```bash
gh pr merge --squash --delete-branch
```

Use `--squash` by default for a clean history. If the repo uses merge commits, the user can specify `--merge` in arguments.

If merge fails:
```
Merge failed: [error message]
Common causes:
- Branch is out of date: run `git pull origin main && git push`
- Merge conflicts: resolve locally and push
- Branch protection rules: check repo settings
```
Stop.

On success:
```
PR #N merged successfully.
Detecting deployment...
```

## Step 4: Detect Deploy Platform

Check for deploy platform configuration:

```bash
# Railway
ls railway.toml 2>/dev/null && echo "RAILWAY"
printenv | grep RAILWAY_ 2>/dev/null | head -3
```

```bash
# Vercel
ls vercel.json .vercel 2>/dev/null && echo "VERCEL"
```

```bash
# Fly.io
ls fly.toml 2>/dev/null && echo "FLY"
```

```bash
# Heroku
ls Procfile 2>/dev/null && git remote -v | grep heroku 2>/dev/null && echo "HEROKU"
```

```bash
# GitHub Pages
gh api repos/{owner}/{repo}/pages --jq '.status' 2>/dev/null && echo "GHPAGES"
```

```bash
# Netlify
ls netlify.toml 2>/dev/null && echo "NETLIFY"
```

If no platform detected:
```
No deployment platform detected. If your app auto-deploys on merge, provide the production URL:
/land-and-deploy --url https://your-app.com
```

## Step 5: Wait for Deploy

### Railway
```bash
# Poll deploy status (Railway deploys on merge to main)
railway status 2>/dev/null
```
Poll every 15 seconds for up to 5 minutes. Railway typically deploys in 1-3 minutes.

### Vercel
```bash
# Check latest deployment
gh api repos/{owner}/{repo}/deployments --jq '.[0] | {state: .statuses_url, environment: .environment, created_at: .created_at}' 2>/dev/null
```

### Fly.io
```bash
fly status 2>/dev/null
```

### GitHub Actions (generic)
```bash
# Watch the deploy workflow
gh run list --limit 1 --json status,conclusion,name,databaseId
```

If the deploy workflow is still running:
```bash
gh run watch [run-id] --exit-status
```

Report progress:
```
Deploy status: [building / deploying / live]
Elapsed: [time since merge]
```

**If deploy fails:**
```
DEPLOY FAILED after [time]

Error: [deploy error if available]

Options:
1. Check deploy logs: [platform-specific command]
2. Revert: /land-and-deploy --revert
3. Fix and re-push: the branch was deleted, create a new one from main
```
Stop.

## Step 6: Post-Deploy Verification

Once the deploy is live:

### Quick smoke test (always)
Try to reach the production URL:
```bash
curl -s -o /dev/null -w "%{http_code} %{time_total}s" <production-url>
```

If the HTTP status is not 200:
```
ALERT: Production returning HTTP [status code]
```

### Canary monitoring (unless --skip-canary)

If a browser tool is available, run a condensed canary check (single pass, not the full monitoring loop):

1. Navigate to the production URL
2. Check for console errors
3. Take a DOM snapshot — verify the page rendered (not blank/error page)
4. Compare against canary baseline if one exists

If no browser tool: "Skipping visual verification (no browser tool available). Run `/browser-reset` to set up browser access."

## Step 7: Revert Mode (--revert)

If the user requested a revert:

```bash
# Find the merge commit
git log --oneline -5 main
```

```bash
# Revert the merge commit
git revert -m 1 HEAD
git push origin main
```

Then wait for the revert to deploy (repeat Step 5).

## Step 8: Deploy Report

```
## Deploy Report

**PR:** #N — [title]
**Branch:** [branch] → [base]
**Merged at:** [timestamp]
**Deploy completed at:** [timestamp]
**Total time (merge → live):** [duration]
**Platform:** [Railway / Vercel / etc.]

### Verification
- HTTP status: [200 OK / error]
- Response time: [Nms]
- Console errors: [none / N new]
- Visual check: [passed / skipped / issues found]

### Result: DEPLOYED SUCCESSFULLY / DEPLOYED WITH WARNINGS / DEPLOY FAILED
```

## Hard Rules
- **Never force-merge** — if CI fails or reviews are missing, stop and explain
- **Always wait for deploy** — don't report success until the deploy is confirmed live
- **Revert is safe** — `git revert` creates a new commit, doesn't rewrite history
- **Default to --squash** — clean history, unless the repo convention says otherwise
