---
description: Use periodically during long sessions or weekly. Analyzes git history for contributor metrics, test health, velocity trends, and team patterns.
---

You are running an engineering retrospective that analyzes git history to produce actionable insights about development patterns, team velocity, and code quality trends.

## Arguments

`$ARGUMENTS` controls the time window and mode:
- Empty or `7d` → last 7 days (default)
- `24h` → last 24 hours
- `14d` → last 14 days
- `30d` → last 30 days
- `compare` → compare current period vs prior period (e.g., this week vs last week)
- A branch name → analyze that branch's commits

## Step 1: Gather Git Data

Run these commands to collect raw data. Adjust the `--since` flag based on the time window.

**Data hygiene (apply to every command below).** Trustworthy numbers require excluding noise consistently:
- **Merge commits:** pass `--no-merges` everywhere — a merge re-attributes thousands of lines to whoever merged.
- **Bot authors:** drop `dependabot`, `renovate`, `github-actions[bot]`, and any `*[bot]` from contributor/churn output (e.g. pipe through `grep -viE '\[bot\]|dependabot|renovate'`).
- **Vendored / generated paths:** exclude them so churn isn't dominated by lockfiles and build output. Set once and reuse as a bash array:
  ```bash
  EXCLUDE=( ":(exclude)**/node_modules/**" ":(exclude)**/dist/**" ":(exclude)**/build/**" ":(exclude)**/vendor/**" ":(exclude)**/*.lock" ":(exclude)**/package-lock.json" ":(exclude)**/*.min.*" ":(exclude)**/__generated__/**" )
  ```
  then append `-- . "${EXCLUDE[@]}"` to the `git log` invocations.
- **Author de-duplication:** one human with two emails is double-counted. If a `.mailmap` exists, git collapses them automatically; if not, note obvious duplicates (same name, different email) in the report instead of treating them as two contributors.

```bash
# Commit log with stats (author, date, files changed, insertions, deletions)
git log --since="7 days ago" --no-merges --format="%H|%an|%ae|%aI|%s" --numstat -- . "${EXCLUDE[@]}"
```

```bash
# Commit count per author
git shortlog --since="7 days ago" -sn --no-merges
```

```bash
# Hotspots — churn (how many revisions touched each file) in the window
git log --since="7 days ago" --no-merges --name-only --format="" -- . "${EXCLUDE[@]}" | sort | uniq -c | sort -rn | head -20

# Long-lived hotspots — the established churn baseline is a 12-month window, not 7 days
git log --since="12 months ago" --no-merges --name-only --format="" -- . "${EXCLUDE[@]}" | sort | uniq -c | sort -rn | head -30
```

**Don't stop at churn.** A high-churn config file or lockfile is not a hotspot. Cross churn WITH size (a cheap proxy for complexity): for each high-churn file get its current line count (`wc -l <file>`) and compute a **hotspot score = revisions × current-LOC**. Only flag files that are high-churn AND high-size (the top-right quadrant) as refactoring priorities; explicitly de-prioritize high-churn/low-size files (config, lockfiles, generated). Use the 12-month view to catch hotspots the short window hides.

```bash
# Test file changes vs total changes
git log --since="7 days ago" --no-merges --name-only --format="" -- . "${EXCLUDE[@]}" | grep -cE '(test_|_test\.|\.test\.|\.spec\.|tests/)' || echo "0"
git log --since="7 days ago" --no-merges --name-only --format="" -- . "${EXCLUDE[@]}" | wc -l
```

```bash
# PR data (if gh CLI available)
gh pr list --state merged --search "merged:>=$(date -v-7d +%Y-%m-%d 2>/dev/null || date -d '7 days ago' +%Y-%m-%d)" --json number,title,author,additions,deletions,changedFiles,mergedAt 2>/dev/null || echo "gh CLI not available"
```

```bash
# Fix/bug commits ratio
git log --since="7 days ago" --oneline --no-merges | grep -ciE '(fix|bug|patch|hotfix)' || echo "0"
git log --since="7 days ago" --oneline --no-merges | wc -l
```

## Step 2: Detect Coding Sessions

Analyze commit timestamps to identify coding sessions. A session is a cluster of commits with gaps < 2 hours between them.

```bash
# Commit timestamps for session detection
git log --since="7 days ago" --format="%an|%aI" --no-merges | sort
```

Group consecutive commits by author where the gap between commits is < 2 hours. Count:
- Number of sessions per contributor
- Average session duration
- Longest session
- Most productive time of day (morning/afternoon/evening/night)

## Step 3: Delivery Metrics (DORA)

Compute the four key delivery metrics — these are the canonical engineering-retrospective signals and are all derivable from the git + gh data above. In compare mode, show each with its period-over-period delta.

**1. Change lead time** — median time from a change's first commit to merge. With gh:
```bash
gh pr list --state merged --search "merged:>=$(date -v-7d +%Y-%m-%d 2>/dev/null || date -d '7 days ago' +%Y-%m-%d)" \
  --json number,mergedAt,commits \
  --jq '.[] | {number, first: .commits[0].committedDate, merged: .mergedAt}' 2>/dev/null || echo "gh CLI not available"
```
For each PR, lead time = `mergedAt − first commit date`; report the **median** (robust to outliers). Without gh, approximate per merged branch as last-commit − first-commit.

**2. Merge / deploy frequency** — merged PRs (or release tags) per period:
```bash
gh pr list --state merged --search "merged:>=<since>" --json number --jq 'length' 2>/dev/null || echo "gh CLI not available"
git tag --sort=-creatordate --format='%(refname:short)|%(creatordate:short)'   # if you tag releases as deploys
```

**3. Change failure rate** — share of merges that needed a fix: `(reverts + hotfixes) ÷ total merges`:
```bash
git log --since="7 days ago" --no-merges --oneline | grep -ciE '\brevert\b|\bhotfix\b|\bfix!' || echo "0"
gh pr list --state merged --search "merged:>=<since> label:incident" --json number --jq 'length' 2>/dev/null || echo "0"
```
Divide by total merges from metric 2.

**4. Failed-deployment recovery time** — median time from a revert/hotfix commit back to the next green state (next successful merge or passing CI). Pair each revert/hotfix with the following recovery commit and report the median gap. If nothing failed, state "no failures this period" — not 0.

Report all four in a table; in compare mode add the delta column. Lower lead time / failure rate / recovery time with steady-or-higher frequency is the healthy direction — but read the four **together as a system**, never collapsed into a single score.

## Step 4: Knowledge, Bus Factor & Suggested Reviewers

Ownership analysis surfaces two risks the raw hotspot count misses: files only one person understands (bus-factor risk) and files churned by many hands (a defect predictor).

For each important file (the hotspots from Step 1, plus any file touched this period), aggregate authorship over full history:
```bash
# Distinct authors and per-author commit counts for a file
git log --no-merges --format="%an" -- <file> | grep -viE '\[bot\]|dependabot|renovate' | sort | uniq -c | sort -rn
```
From this, compute per file:
- **Distinct author count.**
- **Main-dev ownership %** = main author's commits ÷ total commits on the file.

Flag:
- **Single-author / >80%-owned files** that are also hotspots → **bus-factor risk** (knowledge silo; if that person leaves, the file is stranded). Recommend pairing or a knowledge-transfer review.
- **Files with 4+ distinct authors** → **defect-predictor / coordination risk** (Code Maat finds author count correlates with defects). Recommend clearer ownership boundaries.

**Suggested reviewers (optional, actionable).** For the files changed this period — or for a specific branch/PR passed as the argument — the people who have historically edited those files are the best-informed reviewers:
```bash
# Top historical authors across the changed files = suggested reviewers
git log --no-merges --format="%an" -- <changed files...> | grep -viE '\[bot\]|dependabot|renovate' | sort | uniq -c | sort -rn | head -5
```
If a `CODEOWNERS` file exists, cross-check the suggestions against it and note any changed paths that have **no** CODEOWNERS entry (an ownership gap).

## Step 5: Compare Mode (if requested)

If the user asked for `compare`, run the same data collection for the prior period (e.g., if analyzing last 7 days, also collect data for 7-14 days ago).

Calculate deltas:
- Commit velocity: +/-N% vs prior period
- LOC throughput: +/-N%
- Test ratio change: +/-N percentage points
- Fix ratio change: +/-N percentage points
- DORA keys: lead time, merge frequency, change failure rate, recovery time (+/- vs prior period)

## Step 6: Produce Report

Format the report as follows:

```
## Engineering Retrospective — [time window]
**Period:** [start date] to [end date]
**Repo:** [repo name]

> These numbers are heuristics to spark a team conversation — **not** a performance evaluation, ranking, or leaderboard. Commit and LOC counts are gameable (Goodhart's law): treat them as prompts for discussion, never as targets or individual scorecards.

### Team Summary
| Metric | Value | [Trend if compare mode] |
|--------|-------|------------------------|
| Total commits | N | |
| Contributors | N | |
| LOC added | N | |
| LOC removed | N | |
| Net LOC | +/-N | |
| Files changed | N | |
| Test ratio | N% (test files / total files changed) | |
| Fix ratio | N% (fix commits / total commits) | |

### Delivery Metrics (DORA)
| Metric | Value | [Trend if compare mode] |
|--------|-------|------------------------|
| Change lead time (median first-commit → merge) | N hrs/days | |
| Merge / deploy frequency | N per [period] | |
| Change failure rate | N% (reverts+hotfixes / merges) | |
| Failed-deployment recovery time (median) | N hrs / "no failures" | |

### Per-Contributor Breakdown

Present these as team-health observations, **not** a ranking — list contributors alphabetically, not by commit count. Each gets one specific strength and one growth nudge; these describe patterns, not a grade. For each contributor:

**[Name]** — [N] commits, +[added]/-[removed] LOC
- Sessions: [N] sessions, avg [duration], longest [duration]
- Peak hours: [time range]
- Top files: [3 most-touched files]
- Test coverage: [N] test files changed / [N] total files
- [Specific praise: e.g., "Strongest test ratio on the team" or "Shipped the largest feature this period"]
- [Growth opportunity: e.g., "Consider smaller PRs — average was 450 LOC" or "Test ratio below team average"]

### File Hotspots (churn × size)
Rank candidate files by **hotspot score = revisions in window × current LOC**, not raw change count. List the top files in the high-churn AND high-size quadrant — these are the refactoring priorities. Explicitly note and de-prioritize high-churn/low-size files (config, lockfiles, generated): frequent change there is normal, not risk. Include the 12-month view for long-lived hotspots the short window hides.

| File | Revisions | LOC | Hotspot score | Note |
|------|-----------|-----|---------------|------|
| path | N | N | N | high-churn+high-size → refactor / or "config, not a hotspot" |

### Knowledge & Bus Factor
| File | Distinct authors | Main-dev ownership % | Flag |
|------|------------------|----------------------|------|
| path | N | N% | bus-factor risk (single-author hotspot) / coordination risk (4+ authors) / healthy |

- **Bus-factor risks:** [single-author hotspot files — knowledge silos to pair on / transfer]
- **Coordination risks:** [4+ author files — defect predictor; clarify ownership]

### Suggested Reviewers (optional)
For the files changed this period (or the branch/PR argument), the best-informed reviewers by historical authorship:
- `path/area` → [top historical authors]
- CODEOWNERS cross-check: [matches / changed paths with no owner]

### Test Health
- Test ratio this period: N%
- [Trend if compare mode: "Up from N% last period" or "Down from N%"]
- Files with high churn but no corresponding test changes (potential risk)

### PR Summary (if gh data available)
| PR | Author | +/- | Files | Merged |
|----|--------|-----|-------|--------|
| #N | name | +X/-Y | Z | date |

Average PR size: [N] LOC changed

### Observations
- [2-3 specific, actionable observations based on the data]
- [e.g., "3 files changed by all contributors — consider ownership boundaries"]
- [e.g., "Fix ratio is 40% — significant portion of work is bug fixes vs new features"]
```

## Notes
- This command is read-only — it analyzes git history but makes no changes
- All data comes from local git history and optionally the GitHub CLI
- For multi-repo analysis, run `/retro` in each repo separately
- Suggest running `/retro` weekly or at the end of long sessions
- **Delivery Metrics (DORA)** follow the canonical definitions at https://dora.dev/guides/dora-metrics/ — read the four keys *together* as a system; chasing one in isolation (e.g. raw merge frequency) degrades the others.
- **Not a performance review.** Both DORA's "common pitfalls" and Code Maat warn that commit/LOC counts and ownership stats are gameable heuristics, harmful when used to rank or compare people. Use this retro for team-health conversations and process improvement, never as individual evaluation.
- **Data hygiene:** counts exclude merge commits, bot authors, and vendored/generated paths so churn and contributor numbers aren't skewed. If one human commits under two emails, add a `.mailmap` so git collapses them — otherwise they're double-counted.
