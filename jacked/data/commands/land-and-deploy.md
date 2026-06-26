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

If the PR is already merged: skip to Step 4 (diff-scope classification, then deploy detection). Recover `MERGE_SHA` from `gh pr view --json mergeCommit` so revert mode still targets the right commit.

## Step 2: Pre-Merge Readiness Check

Verify the PR is ready to merge:

```bash
# Check CI status
gh pr checks --json name,state,conclusion 2>/dev/null
```

**If any required check is PENDING/QUEUED (not yet a pass or fail), actively wait instead of stopping.** A user who runs this the moment checks queue should be landed, not bounced. Watch CI to completion with a bounded timeout:

```bash
# Wait for CI, fail fast on first failure, cap the wait at 15 minutes
timeout 900 gh pr checks --watch --fail-fast 2>/dev/null
```

Record the CI wait duration for the report. Only **stop** on FAIL (a check concluded failing) or on TIMEOUT (15 min elapsed with checks still pending — report "CI still pending after 15 min" and let the user decide). If `--watch` is unsupported in the installed `gh`, poll `gh pr checks --json name,state,conclusion` every 20s until all states are non-pending or the timeout hits.

```bash
# Check review status
gh pr view --json reviewDecision,reviews 2>/dev/null
```

**Review-staleness check.** An approval that predates later commits may not reflect the code you're about to merge. Find the commit the approving review was submitted against and count code-touching commits since:

```bash
# Newest APPROVED review's commit + commits landed on the branch since then
gh pr view --json reviews,commits 2>/dev/null
```

Take the `commit.oid` (or `submittedAt`) of the latest `state == "APPROVED"` review, then count commits after it that touch code (exclude pure docs/markdown). Classify:
- **CURRENT** — 0–3 commits since the approval (approval still reflects the code)
- **STALE** — 4+ commits, or any commit touching code, landed after the approval (approval may be out of date — flag it)
- **NOT RUN** — no approving review found

Report readiness:
```
PR #N: [title]
- CI checks: [all passing / N failing / waited Xm Ys for pending]
- Reviews: [approved / changes requested / pending] — staleness: [CURRENT / STALE (N commits since approval) / NOT RUN]
- Mergeable: [yes / no — reason]
```

**If CI is failing:** "CI checks are failing. Fix the failures before merging." List the failing checks. Stop.

**If not approved:** "PR needs approval. Request a review or merge manually if you have permission." Stop.

**If approval is STALE:** surface it — "Approval predates N later commits ([list]); the review may not reflect the merged code. Re-request review or confirm before merging." Don't auto-stop, but make the user acknowledge.

**If ready:** proceed to merge.

## Step 3: Merge the PR

```bash
gh pr merge --squash --delete-branch
```

Use `--squash` by default for a clean history. If the repo uses merge commits, the user can specify `--merge` in arguments.

**Merge queue / required auto-merge.** If `gh pr merge --squash` is rejected or queued because the repo enforces a merge queue or required linear history (errors mentioning "merge queue", "not mergeable", "required status", "auto-merge"), don't treat it as a hard failure — fall back to enabling auto-merge and poll until the PR actually merges:

```bash
# Queue the merge, then poll PR state until it lands (cap ~15 min)
gh pr merge --squash --auto --delete-branch
```

Poll `gh pr view --json state,mergedAt,mergeCommit` every 20s. Proceed to deploy detection only once `state == "MERGED"`. If it's still queued after ~15 min, report "PR is queued in the merge queue and hasn't merged yet — deploy will trigger when the queue lands it" and stop rather than mis-reporting a merge that didn't happen.

**Capture the merge-commit SHA** as soon as the PR is merged — revert mode needs the *actual* commit, not a blind `HEAD`:

```bash
# Record the squash/merge commit SHA + timestamp for the report and for revert mode
gh pr view --json mergeCommit,mergedAt --jq '{sha: .mergeCommit.oid, mergedAt: .mergedAt}'
```

Remember this SHA (call it `MERGE_SHA`) — Step 8 reverts *that* commit, not whatever is currently at the tip of main.

If merge fails:
```
Merge failed: [error message]
Common causes:
- Branch is out of date: run `git pull origin main && git push`
- Merge conflicts: resolve locally and push
- Branch protection rules: check repo settings
- Merge queue enabled: retry with `gh pr merge --squash --auto` (see above)
```
Stop.

On success:
```
PR #N merged successfully (commit [MERGE_SHA]).
Detecting deployment...
```

## Step 4: Classify Diff Scope

Don't run the same heavy verification regardless of what changed. Classify what the PR actually touched, then scale the deploy-wait and verification depth to match:

```bash
# Files changed in the merged PR
gh pr diff --name-only 2>/dev/null
```

Bucket the changed paths:
- **docs** — only `*.md`, `*.html`, `docs/`, `LICENSE`, images, etc. (no executable code)
- **config** — only `*.toml`, `*.yml`, `*.yaml`, `*.json`, `.env*`, CI workflows, infra/manifest files (no app logic)
- **backend** — server/API/library code, migrations, server-rendered code with no client-visible surface
- **frontend** — any `*.js`, `*.jsx`, `*.ts`, `*.tsx`, `*.css`, `*.scss`, `*.vue`, `*.svelte`, templates, or other client-rendered UI

Pick the **highest-verification bucket present** (frontend > backend > config > docs) and scale accordingly:
- **docs-only** → skip deploy-wait and visual verification entirely; note "docs-only change, no runtime verification needed." (If the platform still rebuilds on every merge, a single HTTP-200 smoke is enough.)
- **config-only or backend-only** → wait for deploy, then HTTP-200 + health/non-blank smoke only (Step 6). Skip the browser/visual canary — there's no client surface a visual check would exercise.
- **any frontend change** → full path: deploy-wait + smoke + the condensed visual canary.

Report the classification:
```
Diff scope: [docs / config / backend / frontend] — verification level: [none / smoke-only / full]
```

## Step 5: Detect Deploy Platform

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

## Step 6: Wait for Deploy

Skip this step entirely for a **docs-only** diff (Step 4) — there's nothing runtime to wait on.

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
2. Revert this deploy (recommended): /land-and-deploy --revert
3. Fix and re-push: the branch was deleted, create a new one from main
```
Stop. (Revert here targets the captured MERGE_SHA — see Step 8.)

## Step 7: Post-Deploy Verification

Once the deploy is live:

### Quick smoke test (always)

A 200 on `/` can still serve a blank SPA shell or a styled error page, so don't stop at the root curl. Probe the root, then a health endpoint, then confirm the body isn't empty:

```bash
# Root URL: status + latency + body byte size
curl -s -o /tmp/land_root.html -w "root: %{http_code} %{time_total}s %{size_download}B\n" <production-url>
```

```bash
# Health endpoint: try /health, then /api/health (a 404 on both is fine — many apps have neither)
curl -s -o /dev/null -w "/health: %{http_code}\n"      <production-url>/health     2>/dev/null
curl -s -o /dev/null -w "/api/health: %{http_code}\n"  <production-url>/api/health 2>/dev/null
```

Pass criteria:
- Root returns **200**, **and**
- Body is non-blank — `size_download` above a small threshold (e.g. > 200 bytes; a near-empty body usually means a broken build or a blank SPA shell), **and**
- If a `/health` or `/api/health` endpoint responds at all, it returns **200** (a non-200 health endpoint is a hard fail even if `/` looks fine).

If any criterion fails:
```
ALERT: Production smoke check failed
- Root: HTTP [status], [bytes]B
- Health: [/health status, /api/health status]

Options:
1. Investigate: open the URL / check logs
2. Revert this deploy: /land-and-deploy --revert
3. Continue anyway (you accept the risk)
```

### Canary monitoring (unless --skip-canary)

Skip the visual canary for **config-only / backend-only** diffs (Step 4) — there's no client surface to inspect; the smoke test above is sufficient.

For frontend changes, if a browser tool is available, run a condensed canary check (single pass, not the full monitoring loop):

1. Navigate to the production URL
2. Check for console errors
3. Take a DOM snapshot — verify the page rendered (not blank/error page)
4. Compare against canary baseline if one exists

If no browser tool: "Skipping visual verification (no browser tool available). Run `/browser-reset` to set up browser access."

If the canary surfaces a regression:
```
CANARY ALERT: [console errors / blank render / perf regression]

Options:
1. Investigate with the full loop: /canary
2. Revert this deploy: /land-and-deploy --revert
3. Accept and continue
```

## Step 8: Revert Mode (--revert)

If the user requested a revert, target the **specific merge commit captured in Step 3** (`MERGE_SHA`), not a blind `HEAD` — if anything else merged to main after ours, `HEAD` is the wrong commit:

```bash
# Reconfirm the merge commit if MERGE_SHA isn't already in hand
gh pr view <PR> --json mergeCommit --jq '.mergeCommit.oid'   # or: git log --oneline -10 main
```

**Before reverting, check the diff for schema/data migrations** — `git revert` reverts *code* but does NOT undo an already-applied schema change, so a reverted app can end up running against a forward-migrated database:

```bash
# Detect migration files in the merged diff
gh pr diff <PR> --name-only 2>/dev/null | grep -Ei 'migrations/|alembic/|prisma/migrations|db/migrate|\.sql$'
```

If any match:
```
WARNING: This PR includes database/schema migrations ([files]).
`git revert` undoes the code but NOT the applied schema change. Before reverting:
- Confirm the migration is backward-compatible (the old code can run against the new schema), OR
- Apply the corresponding down-migration / restore from backup as a separate, planned step.
Reverting code alone may leave the app broken against a forward-migrated DB.
```
Make the user acknowledge before proceeding.

```bash
# Squash merge (the default here) = a normal single-parent commit; revert it directly:
git revert <MERGE_SHA>
# True merge commit (repo uses --merge) = two parents; pick mainline with -m 1:
#   git revert -m 1 <MERGE_SHA>
git push origin main
```

`git revert -m 1` ONLY applies to a real two-parent merge commit — running it on a squash/normal commit errors with "mainline was specified but commit is not a merge." Since this command defaults to `--squash`, use plain `git revert <MERGE_SHA>` unless the repo merged with `--merge`.

**If branch protection blocks a direct push to main**, don't force it — open a revert PR instead and merge it through the normal gate:

```bash
gh pr create --title "Revert: <PR title>" --body "Reverts #<PR> (deploy regression)" --base main
# then land it: gh pr merge --squash --auto  (or re-run /land-and-deploy on the revert PR)
```

Then wait for the revert to deploy (repeat Step 6).

## Step 9: Deploy Report

```
## Deploy Report

**PR:** #N — [title]
**Branch:** [branch] → [base]
**Merge commit:** [MERGE_SHA]
**Diff scope:** [docs / config / backend / frontend] — verification: [none / smoke-only / full]
**CI wait:** [none / waited Xm Ys for pending checks]
**Merged at:** [timestamp]
**Deploy completed at:** [timestamp]
**Total time (merge → live):** [duration]
**Platform:** [Railway / Vercel / etc.]

### Verification
- HTTP status: [200 OK / error]
- Response time: [Nms]
- Body size: [Nbytes / non-blank]
- Health endpoint: [/health or /api/health 200 / not present]
- Console errors: [none / N new]
- Visual check: [passed / skipped (backend/config or no browser) / issues found]

### Result: DEPLOYED SUCCESSFULLY / DEPLOYED WITH WARNINGS / DEPLOY FAILED
```

## Hard Rules
- **Never force-merge** — if CI fails or reviews are missing, stop and explain
- **Wait for pending CI, don't bail** — if required checks are still queued, watch them to completion (bounded 15-min timeout); only stop on a real FAIL or timeout
- **Always wait for deploy** — don't report success until the deploy is confirmed live (except docs-only diffs, which have nothing to wait on)
- **Revert the captured merge SHA, not HEAD** — `git revert <MERGE_SHA>` (add `-m 1` only for a true two-parent merge commit) targets the actual commit; HEAD is wrong if anything merged after ours
- **Revert is history-safe but NOT schema-safe** — `git revert` creates a new commit (no history rewrite), but it does NOT undo applied database/schema migrations. If the diff touched migrations, warn and require a backward-compatible migration or a planned down-migration/restore before reverting
- **Default to --squash** — clean history, unless the repo convention says otherwise
