---
description: "Engineering retrospective — analyzes git history for contributor metrics, test health, velocity trends, and team patterns"
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

```bash
# Commit log with stats (author, date, files changed, insertions, deletions)
git log --since="7 days ago" --format="%H|%an|%ae|%aI|%s" --numstat
```

```bash
# Commit count per author
git shortlog --since="7 days ago" -sn --no-merges
```

```bash
# Files most frequently changed (hotspots)
git log --since="7 days ago" --name-only --format="" | sort | uniq -c | sort -rn | head -20
```

```bash
# Test file changes vs total changes
git log --since="7 days ago" --name-only --format="" | grep -cE '(test_|_test\.|\.test\.|\.spec\.|tests/)' || echo "0"
git log --since="7 days ago" --name-only --format="" | wc -l
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

## Step 3: Compare Mode (if requested)

If the user asked for `compare`, run the same data collection for the prior period (e.g., if analyzing last 7 days, also collect data for 7-14 days ago).

Calculate deltas:
- Commit velocity: +/-N% vs prior period
- LOC throughput: +/-N%
- Test ratio change: +/-N percentage points
- Fix ratio change: +/-N percentage points

## Step 4: Produce Report

Format the report as follows:

```
## Engineering Retrospective — [time window]
**Period:** [start date] to [end date]
**Repo:** [repo name]

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

### Per-Contributor Breakdown

For each contributor:

**[Name]** — [N] commits, +[added]/-[removed] LOC
- Sessions: [N] sessions, avg [duration], longest [duration]
- Peak hours: [time range]
- Top files: [3 most-touched files]
- Test coverage: [N] test files changed / [N] total files
- [Specific praise: e.g., "Strongest test ratio on the team" or "Shipped the largest feature this period"]
- [Growth opportunity: e.g., "Consider smaller PRs — average was 450 LOC" or "Test ratio below team average"]

### File Hotspots
Top 10 most frequently changed files. Files touched by 3+ contributors or in 5+ commits are flagged as potential coordination risks.

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
