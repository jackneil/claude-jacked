---
description: Verify git hygiene and safely clean branches, stashes, stale worktrees, and untracked state in any repo. Report-first, confirm-each, never loses work.
argument-hint: "[--dry-run | --auto-safe]"
model: opus
disable-model-invocation: true
---

You are running a safe git-hygiene cleanup for the repository in the current working directory.
Mode = the value of `$ARGUMENTS` (empty | `--dry-run` | `--auto-safe`).

The user's goal, verbatim: **"work tree is clean, nothing stale/untracked/stashed, PRs all set up, PRs all merged to master, nothing hanging out there."**

Your job is to VERIFY that state and help reach it **without ever silently modifying the repo and without ever losing work.** This is REPORT, then CONFIRM EACH.

---

## How this stays safe (read first)

This command is **report-first, confirm-each.** The gate that protects the user is THIS prompt's safety rules, applied step by step — not any tool allowlist.

- **The scan is 100% read-only.** Everything in the Scan section only inspects (`status`, `log`, `for-each-ref`, `rev-list`, `gh pr list`, …). If the session prompts before running them, they are safe to approve.
- **Nothing destructive runs without an explicit, per-item "yes."** For each cleanup candidate you echo the EXACT command and its evidence, then wait. You never present a single "clean everything?" prompt.
- **Each destructive command also surfaces the normal Claude Code permission prompt** as an independent backstop. Tell the user to expect that second confirmation on `git fetch`, `git branch -d`/`-D`, `git stash drop`, `git clean -fd`, `git worktree prune`, etc. — it is the safety design, not a bug.
- **Never tell the user to pre-authorize destructive git for this command.** Do not advise adding a broad `Bash(git:*)`/`Bash(git branch:*)` always-allow rule, and do not advise clicking "always allow" on `git branch -d`, `git stash drop`, `git clean`, or `git fetch` — that removes the backstop. If such a rule is already active, warn the user before proceeding.

---

## Safety rules (NON-NEGOTIABLE — re-read before any action)

1. **Read-only by default; every destructive step is opt-in.** No-arg and `--dry-run` run ZERO mutating git operations. `--auto-safe` may auto-execute ONLY two provably-safe classes, and ONLY when remote-tracking refs were refreshed THIS run (see rule #9): (a) prune local branches whose upstream is confirmed `[gone]` AND that are fully merged into the **remote** default branch, (b) `git branch -d` branches confirmed merged into the **remote** default branch. Everything else still requires explicit per-item confirmation.
2. **No bulk "clean everything?" prompt.** One confirmation authorizes at most ONE destructive action on ONE named target. Echo the EXACT command first, get an explicit "yes" for THAT command, then run it. Declining one item never cascades into auto-approving others.
3. **NEVER `git branch -D` (force-delete) automatically.** Force-delete requires explicit per-branch confirmation and its own permission prompt; never auto-run it (not even under `--auto-safe`). Branch deletion uses ONLY `git branch -d`. If `-d` refuses, that is a STOP signal you report — never an invitation to escalate to `-D`. The single exception: a branch whose PR `gh` confirms is **MERGED** but which `-d` refuses because of a squash/rebase merge — only then may you PROPOSE `-D`, and only with explicit per-branch confirmation, only after BOTH of these prove there is no post-merge work:
   - `git rev-list --count <branch> ^<REMOTE-DEFAULT>` is `0` against the FRESHLY-FETCHED remote default (the branch tip is fully contained in the remote default), **OR** `git log --oneline --since="<pr.mergedAt>" <branch>` is empty; AND
   - the merged PR's `headRefName` matches this exact branch AND `headRepositoryOwner`/`headRepository` matches this repo (guard against a recycled same-named branch inheriting a stale MERGED PR).
   `git log -1 <branch>` is NOT acceptable proof — it shows only the tip and hides stacked post-merge commits. If ANY commit exists past the merge point, KEEP.
4. **NEVER drop, clear, or pop a stash as a cleanup action.** No `git stash drop`, `git stash clear`, `git stash pop`. Stashes are uncommitted work = KEEP. To remove one, show `git stash show -p stash@{N}` first and require the user to confirm that exact `git stash drop stash@{N}` (and approve its permission prompt). Never `git stash clear`.
5. **NEVER `git clean` without first printing the full `git clean -nd` preview** (and `-ndX` separately if ignored-file review is even being considered) and confirming each path class. NEVER use `-x` or `-X` by default — ignored files include `.env`, local config, credentials, installed hooks. Untracked files are NEVER part of `--auto-safe`. Running `git clean -fd` requires explicit confirmation and a permission prompt.
6. **NEVER discard uncommitted work.** No `git reset --hard`, `git checkout -- .`, `git checkout <ref>` over a dirty tree, `git restore`. Modified/staged/untracked files are reported as "needs-attention — your work, untouched."
7. **NEVER delete/reset/switch the default branch or the current branch, and NEVER `git branch -d/-D` a branch while it is still checked out in ANY worktree.** Resolve the default branch dynamically — never hardcode `origin/main`. Build the worktree-held-branch exclusion set deterministically (see Scan + the exclusion recipe in Report) and subtract it from every *branch*-delete candidate. To clean a STALE worktree, the worktree is removed FIRST (`git worktree remove`, only once the per-worktree pass proved it CLEAN — no staged/unstaged/untracked) which frees its branch; ONLY THEN may the now-detached branch be deleted under the normal merged/PR rules. NEVER remove a worktree that is dirty, has untracked files, or has unpushed commits, and NEVER remove the current or the default worktree. `git worktree remove` requires explicit per-worktree confirmation and its own permission prompt — it is never part of `--auto-safe`.
8. **NEVER push, force-push, `--force-with-lease`, or delete a remote branch/ref.** Unpushed work is reported ("N commits unpushed on branch X"); pushing is the user's decision. Cleanup never mutates any remote.
9. **A successful `git fetch --prune` THIS run is a HARD PRECONDITION for judging any branch merged/gone/safe and for ANY deletion (auto-safe or confirmed).** Fetch is a NETWORK op, so it is itself opt-in and confirmable. If the user declines the fetch, or no remote refresh happened this run (`--dry-run`, no remote, offline), you MUST: label every merge/gone/ahead-behind verdict "provisional — remote data is stale", DISABLE every 🔴 cleanup candidate that depends on remote state (no remote-dependent deletions, including all of `--auto-safe`'s auto-deletions — they fall through to manual or are skipped entirely), and never delete on stale remote-tracking refs.
10. **Detached HEAD / shallow clone / no remote / no gh → degrade, never guess.** In detached HEAD, warn first and refuse anything that could orphan the current commit (advise `git switch -c <name>`). On a shallow clone, ancestry merge-detection is unreliable — prefer PR state, else KEEP. With no remote, skip fetch/PR/upstream logic and never delete merged-by-local-ancestry branches (their commits may live only locally). With gh absent/unauthenticated, NEVER infer merged from local ancestry alone — report "cannot confirm PR merge state" and KEEP.
11. **NEVER emit a false "all clean."** The green verdict is allowed ONLY when EVERY condition below is verified this run: clean work tree in the current AND every other worktree, no untracked (or explicitly acknowledged), zero stashes, no unpushed commits on ANY local branch or worktree, no local-only branch holding unique commits (unless intentionally retained), every pushed branch has a PR and that PR is MERGED (or intentionally retained), no open/draft/blocked/CI-failing PR on any branch, no detached-HEAD dangling commits, no unpushed tags, AND remote-tracking data freshly fetched this run. If ANY is unverified (no fetch, gh unavailable, offline, shallow, an un-inspected worktree), the summary says **"cannot confirm clean — <specific reasons>"**, never "all clean." Ignored build artifacts are NOT required to be absent, but the green line must DISCLOSE them ("tracked tree clean; N ignored artifacts present, not evaluated") rather than imply they don't exist.
12. **When in doubt about ANY classification, default to KEEP and report.** Never delete.

---

## Scan (read-only inspection — RUN THESE YOURSELF; everything guarded so missing gh / no remote / not-a-repo degrade gracefully)

Run the commands below to gather state. They are ALL read-only — you may batch them into one or a few Bash calls and approve when prompted. Then interpret per the short-circuits; skip probes a short-circuit makes irrelevant (e.g. STOP on `NOT_A_GIT_REPO`; skip the gh/PR probes when gh is absent). Each command is self-guarding (`2>/dev/null`, `|| echo …`) so a missing tool or remote degrades to a sentinel rather than aborting. Interpret each command's output directly — avoid re-deriving it with extra shell; the few `sed`/`awk`/`comm` uses below are where a deterministic extraction matters.

- In a git repo? `git rev-parse --is-inside-work-tree 2>/dev/null || echo NOT_A_GIT_REPO`
- Bare repo? `git rev-parse --is-bare-repository 2>/dev/null || echo unknown`
- Inside .git dir (cd'd into .git)? `git rev-parse --is-inside-git-dir 2>/dev/null || echo unknown`
- Toplevel: `git rev-parse --show-toplevel 2>/dev/null || echo .`
- Shallow clone? `git rev-parse --is-shallow-repository 2>/dev/null || echo false`
- HEAD state: `git symbolic-ref --quiet HEAD >/dev/null 2>&1 && echo ATTACHED || echo DETACHED_HEAD`
- Current branch (or short SHA if detached): `git symbolic-ref --quiet --short HEAD 2>/dev/null || git rev-parse --short HEAD 2>/dev/null || echo unknown`
- Has any commits? `git rev-parse --verify --quiet HEAD >/dev/null 2>&1 && echo yes || echo UNBORN_HEAD`
- Op in progress (rebase/merge/cherry-pick)? `test -d "$(git rev-parse --git-path rebase-merge 2>/dev/null)" -o -d "$(git rev-parse --git-path rebase-apply 2>/dev/null)" -o -f "$(git rev-parse --git-path MERGE_HEAD 2>/dev/null)" -o -f "$(git rev-parse --git-path CHERRY_PICK_HEAD 2>/dev/null)" && echo IN_PROGRESS || echo clean`
- Remote names: `git remote 2>/dev/null || echo '(no remotes)'`
- Remote URLs (credentials redacted): `git remote 2>/dev/null | while read r; do printf '%s ' "$r"; git remote get-url "$r" 2>/dev/null | sed -E 's#(https?://)[^@/]*@#\1***@#'; done || echo '(no remotes)'`
- Remote count (0 = no remote): `git remote 2>/dev/null | awk 'END{print NR+0}'`
- Default remote (origin if present, else first): `git remote 2>/dev/null | grep -qx origin && echo origin || git remote 2>/dev/null | head -1 || echo '(none)'`
- Default-remote HEAD set? `R=$(git remote 2>/dev/null | grep -qx origin && echo origin || git remote 2>/dev/null | head -1); git symbolic-ref --quiet "refs/remotes/$R/HEAD" >/dev/null 2>&1 && echo yes || echo no`
- Default branch from <default-remote>/HEAD: `R=$(git remote 2>/dev/null | grep -qx origin && echo origin || git remote 2>/dev/null | head -1); git symbolic-ref --quiet --short "refs/remotes/$R/HEAD" 2>/dev/null | sed "s@^$R/@@" || echo '(unset)'`
- Default branch fallback (local main/master/trunk/develop — for REPORTING only, never to authorize deletes): `out=$(for b in main master trunk develop; do git show-ref --verify --quiet "refs/heads/$b" && echo "local:$b"; done 2>/dev/null); [ -n "$out" ] && printf '%s\n' "$out" || echo none`
- Working-tree snapshot (current worktree): `git status --porcelain=v2 --branch --show-stash 2>/dev/null || git status --porcelain 2>/dev/null || echo '__ERR__status'`
- Staged changes: `git diff --cached --name-status 2>/dev/null | head -50 || echo '__ERR__diff'`
- Unstaged changes: `git diff --name-status 2>/dev/null | head -50 || echo '__ERR__diff'`
- Untracked files: `git ls-files --others --exclude-standard 2>/dev/null | head -50 || echo '__ERR__lsfiles'`
- Ignored artifacts (informational only): `git status --porcelain --ignored 2>/dev/null | awk '/^!! /{sub(/^!! /,""); print}' | head -30 || echo '(none)'`
- Ignored artifact count: `git status --porcelain --ignored 2>/dev/null | awk '/^!! /{n++} END{print n+0}'`
- Stashes (with age): `git stash list --format='%gd | %ci | %gs' 2>/dev/null || echo '(no stashes)'`
- All local branches (name|upstream|track-nobracket|age): `git for-each-ref --format='%(refname:short)|%(upstream:short)|%(upstream:track,nobracket)|%(committerdate:relative)' refs/heads/ 2>/dev/null || echo '__ERR__refs'`
- Branches with NO upstream: `git for-each-ref --format='%(refname:short)|%(upstream)' refs/heads/ 2>/dev/null | awk -F'|' '$2==""{print $1}' || echo '__ERR__refs'`
- Branches with [gone] upstream: `git for-each-ref --format='%(refname:short) %(upstream:track)' refs/heads/ 2>/dev/null | awk '$2=="[gone]"{print $1}' || echo '__ERR__refs'`
- Current branch ahead/behind upstream (prints `behind<TAB>ahead`): `git rev-parse --abbrev-ref --symbolic-full-name @{upstream} >/dev/null 2>&1 && git rev-list --left-right --count @{upstream}...HEAD 2>/dev/null || echo NO_UPSTREAM`
- Worktrees (porcelain): `git worktree list --porcelain 2>/dev/null || echo '(none)'`
- Worktree-held branches (exclusion set — exact extraction): `git worktree list --porcelain 2>/dev/null | sed -n 's@^branch refs/heads/@@p' || echo '(none)'`
- Worktree paths (to inspect each for dirty/unpushed state): `git worktree list --porcelain 2>/dev/null | sed -n 's@^worktree @@p' || echo '(none)'`
- Worktree prune preview (dirs already gone from disk): `git worktree prune --dry-run -v 2>/dev/null || echo '(none)'`
- Submodules: `test -f .gitmodules && git submodule status --recursive 2>/dev/null || echo '(no submodules)'`
- Local tags: `git tag 2>/dev/null | head -30 || echo '(none)'`
- Last fetch (relative): `f="$(git rev-parse --git-path FETCH_HEAD 2>/dev/null)"; test -f "$f" && git log -1 --format=%cr 2>/dev/null || echo 'never fetched'`
- gh installed? `command -v gh >/dev/null 2>&1 && echo yes || echo no`
- gh authenticated? `command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1 && echo yes || echo no`
- gh resolves this repo (GitHub remote)? `command -v gh >/dev/null 2>&1 && gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || echo none`
- gh user: `command -v gh >/dev/null 2>&1 && gh api user -q .login 2>/dev/null || echo unknown`
- ALL open PRs (any author — completeness authority): `command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1 && gh pr list --state open --limit 200 --json number,title,author,headRefName,isDraft,mergeable,mergeStateStatus,reviewDecision,statusCheckRollup,url 2>/dev/null || echo '__GH_SKIP__'`
- My open PRs (convenience view only): `command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1 && gh pr list --author "@me" --state open --json number,title,headRefName,url 2>/dev/null || echo '__GH_SKIP__'`

> **Per-branch PR existence, per-branch unpushed counts, per-worktree dirty/ahead state, and unpushed-tag detection are NOT gathered above** — they require looping over the branch/worktree lists. Compute them in the Report phase using the recipes given there. They are all read-only.

### Interpret the scan first — short-circuits

- **`NOT_A_GIT_REPO`** → print "Not a git repository — nothing to clean up." and STOP. Do nothing else.
- Any **`__ERR__*`** sentinel → the underlying git command FAILED (not "empty"). Report it as an inspection gap; do not treat as "clean."
- **Inside `.git` dir = true** → warn the user they're inside `.git`; reason relative to the toplevel above.
- **Bare repo = true** → skip ALL working-tree checks (no tree to be dirty); only branch/remote/stash checks apply.
- **`UNBORN_HEAD`** → skip merged/ahead-behind/branch classification; only staged + untracked matter.
- **`IN_PROGRESS`** → a rebase/merge/cherry-pick is underway. HALT all cleanup. Report only, and tell the user to finish or abort the in-progress operation first.
- **`DETACHED_HEAD`** → emit a prominent needs-attention note: commits here belong to no branch and are GC-able. Skip current-branch upstream/PR checks. Refuse any branch-switch/delete/clean. Advise `git switch -c <name>` to save the work first.
- **Remote count `0`** → skip all fetch / ahead-behind-vs-upstream / `[gone]` / PR logic. Local-only branches are informational (nowhere to push). Never nag to push. No remote-dependent deletions are possible (rule #9/#10).
- **Shallow = true** → downgrade every "merged → safe to delete" verdict to needs-attention with a "shallow clone — cannot prove merge" caveat; prefer PR state.
- **gh `no` / not authed / repo resolves `none`** → skip the entire PR category with one note ("gh CLI not installed" / "gh not authenticated — run `gh auth login`" / "remote is not a GitHub repo"). NEVER infer merged from local ancestry alone in this case, and the green verdict is NOT available (PR state unverifiable).
- **Default-remote HEAD = no** (but a remote exists) → low-priority needs-attention: suggest `git remote set-head <default-remote> -a` (offer, do NOT auto-run — it needs confirmation) so default-branch detection is reliable.

**Resolve the default branch and remote (cache both):**
1. **Default remote** = `origin` if it exists, else the first remote (already computed above). Use this name everywhere — do NOT hardcode `origin`.
2. **Default branch** = first non-empty of: `<default-remote>/HEAD` short name → local main/master/trunk/develop (REPORTING ONLY) → current branch.
3. **For any DELETION decision, the ancestry test MUST key off the remote-tracking default `refs/remotes/<default-remote>/<default-branch>` and that ref must have been fetched THIS run.** If only a local default is available (no remote, or unfetched), downgrade ALL merged verdicts to needs-attention/KEEP — exactly like the shallow-clone rule. A local default that is itself ahead of (unpushed vs) the remote default must NOT authorize deletes, because the merged-in work may live only locally.
4. Report WHICH default-branch source was used and whether it is remote-fetched-this-run vs local-only, so the user can trust (or distrust) the verdicts.

Before running any `git rev-list ... ^<REF>`, verify the ref exists: `git rev-parse --verify --quiet <REF>` (a bad ref makes rev-list fatal). Guard `@{upstream}`/`@{push}` usage with `git rev-parse --abbrev-ref --symbolic-full-name @{upstream} >/dev/null 2>&1` first — these abort with exit 128 on no-upstream branches.

---

## Report (present ONE categorized findings table)

First, run the per-branch / per-worktree passes the Scan deferred. Use the cached default-remote `R` and default-branch `D`.

**A. Per-branch unpushed + PR + unique-commit pass** — for EVERY local branch (you already have the name|upstream|track list):
- **Unpushed commits:** if the branch HAS an upstream, count ahead via the `%(upstream:track,nobracket)` field if populated, else `git rev-list --left-right --count <upstream>...<branch>` (ahead = right number). If the branch has NO upstream, the unpushed check cannot run — route to needs-attention "no upstream — cannot confirm pushed", never to a delete candidate.
- **Unique commits vs default (only if `refs/remotes/R/D` resolves and was fetched this run):** `git rev-list --count <branch> ^refs/remotes/R/D`. `0` = contained in the remote default. For a no-upstream branch, `0` here does NOT mean "safe to delete" (work may exist only locally) — keep it in needs-attention. `>0` on a no-upstream branch → "local-only branch with N unique commits — not pushed, no PR."
- **PR existence (if gh available):** `gh pr list --head <branch> --state all --json number,state,mergedAt,isDraft,reviewDecision,mergeStateStatus,statusCheckRollup,headRefName,headRepositoryOwner,url`. No matching PR on a pushed branch → "no PR set up." Only DRAFT/OPEN → "PR not merged." Use this per-branch lookup (not a global recent-merged window) so busy repos and renamed squash-merged branches don't slip through.

**B. Per-worktree pass** — for EACH worktree path from the Scan (including the current one): `git -C <path> status --porcelain` (dirty?) and, for its checked-out branch, the same ahead/behind check. Surface any dirty or ahead worktree in NEEDS ATTENTION. Rule #11's "clean in every worktree" is unverifiable without this.

For each NON-current, NON-default worktree ALSO compute (using cached default-remote `R` / default-branch `D`, only when `refs/remotes/R/D` resolves and was fetched this run): `git rev-list --count <branch> ^refs/remotes/R/D` (`0` = contained in the remote default) and the branch's `gh` PR state. Then classify it — this is how STALE worktrees (a still-present dir whose work has landed or been abandoned) get surfaced instead of lingering forever:
- **dirty / has untracked / unpushed-ahead** → NEEDS ATTENTION, never remove — it holds your work.
- **clean AND (merged into remote default by ancestry OR a `gh`-confirmed MERGED PR)** → 🔴 removal candidate (see the worktree bullet below).
- **clean BUT the branch has N unique commits and NO merged PR** → NEEDS ATTENTION (NOT an auto-candidate): "superseded? present worktree `<path>`, branch `<branch>` is clean but has N commit(s) not in `<remote-default>` and no merged PR. If you confirm the feature shipped elsewhere (a rewrite/squash landed under a different branch), remove with `git worktree remove <path>` then `git branch -D <branch>`; otherwise KEEP." Abandoned-but-unique work is the user's call — never auto-remove it.

**C. Unpushed-tags pass (if a remote exists and was fetched this run):** `comm -23 <(git tag | sort) <(git ls-remote --tags <R> 2>/dev/null | sed 's,.*refs/tags/,,;s,\^{},,' | sort -u)`. Any line = a local tag absent from the remote → NEEDS ATTENTION "unpushed tag." Skip (and note "not checked") if no remote or no fetch.

**D. Open-PR reconciliation** — from the ALL-open-PRs scan (any author), each open PR whose head is a branch in this repo means "not all merged." Classify: CONFLICTING/`mergeStateStatus=DIRTY` → conflict; `reviewDecision=CHANGES_REQUESTED` → changes requested; `reviewDecision` review pending/required → review pending; `statusCheckRollup` failing → CI failing; `mergeStateStatus=BLOCKED` → blocked. The `@me` list is a convenience subsection only — never the authority for the verdict.

Now produce a single table with three buckets. Note at the top: the resolved default branch + how it was detected (and remote-fetched-this-run vs local-only), the mode, and the fetch-staleness caveat. Cap long lists at ~50 entries with a "+N more" note.

- **✅ CLEAN** — one summary line per healthy area (work tree clean in all worktrees; no stashes; branch pushed & merged; no untracked; PRs merged). Only mark an area clean if actually verified this run. If ignored artifacts exist, the clean work-tree line must say "tracked tree clean (N ignored artifacts present — not evaluated)".
- **⚠️ NEEDS ATTENTION** — your work, untouched. Rows for: staged/unstaged/conflicted files (conflicts = highest priority), untracked files, stashes, branches ahead of upstream (unpushed, with N), branches behind (suggest `git pull --ff-only`), diverged branches, no-upstream branches (esp. with unique commits), local-only branches with unique commits, pushed branches with NO PR ("PRs all set up" failure), open/draft PRs, PRs CONFLICTING / CHANGES_REQUESTED / review-pending / CI-failing / BLOCKED merge state, closed-not-merged PRs, dirty or ahead OTHER worktrees, clean-but-superseded present worktrees (branch has unique commits + no merged PR — confirm before removing), uninitialized/modified submodules, unpushed tags, detached HEAD, stale fetch, default-remote HEAD unset. NEVER propose deleting/discarding any of these.
- **🔴 CLEANUP CANDIDATE** — each row carries the EXACT proposed command and its EVIDENCE. Eligible items only (and ALL remote-dependent rows are DISABLED if no fetch happened this run — rule #9):
  - `[gone]` upstream branches with `git rev-list --count <branch> ^refs/remotes/R/D` == 0 (against the freshly-fetched remote default) → `git branch -d <branch>`
  - Branches merged into the **remote** default (`rev-list count == 0` vs `refs/remotes/R/D`, fetched this run) → `git branch -d <branch>`
  - Branches whose PR `gh` confirms MERGED but `-d` refuses (squash/rebase) → PROPOSE `git branch -D <branch>` ONLY with the full rule-#3 proof (post-merge-empty + headRef/repo match); show evidence "PR #N '<title>' merged <date>"; never auto-run, always per-branch confirm
  - Stashes → review with `git stash show -p stash@{N}`, then `git stash drop stash@{N}` (per-stash, never clear)
  - Untracked files → `git clean -nd` preview first, then `git clean -fd <path>` per path class (never `-x`/`-X` by default)
  - Ignored artifacts (optional) → `git clean -ndX` preview first (call out `.env`/config/hook danger)
  - Prunable worktrees (dir already gone from disk) → `git worktree prune`
  - Stale PRESENT worktrees (dir still on disk) — ONLY when the per-worktree pass (B) proved the worktree CLEAN (no staged/unstaged/untracked), it is neither the current nor the default worktree, AND its branch is either merged into the remote default by ancestry (`git rev-list --count <branch> ^refs/remotes/R/D` == 0, fetched this run) or a `gh`-confirmed MERGED PR → PROPOSE, per worktree, in THIS order: `git worktree remove <path>` THEN `git branch -d <branch>` (`-D` only via the rule-#3 PR-merged proof). Removing the worktree first frees the branch without violating rule #7. Evidence: "clean worktree, branch merged into `<remote-default>` (0 unique)" or "PR #N '<title>' merged". A clean-but-superseded worktree (unique commits, no merged PR) is NOT eligible here — it is reported in NEEDS ATTENTION for the user to confirm.

**Exclusion set before listing ANY branch as a delete candidate:** subtract the union of {current branch, default branch, every worktree-held branch from the deterministic `sed` extraction in the Scan, and any branch whose worktree dir is in the prune preview (gone-but-not-pruned)}. A `--no-merged` branch with an OPEN PR is EXPECTED — list it informationally, not as a problem. `git branch -d`'s own refusal is the final backstop — never escalate to `-D` to get around it (except via the rule-#3 PR-merged path).

---

## Cleanup (REPORT, THEN CONFIRM EACH — honour `$ARGUMENTS`)

**If `$ARGUMENTS` contains `--dry-run`:** stop after the Report. Print the exact commands you WOULD propose, run NOTHING, do not fetch. Label all merge/gone verdicts provisional; no 🔴 row is actionable.

**Otherwise** (empty or `--auto-safe`):

1. **Offer the fetch — and gate everything on it.** If a remote exists and fetch is stale (or you simply have not fetched this run), ask: "Run `git fetch --prune <default-remote>`? (network op; refreshes gone/merged/ahead-behind — and is REQUIRED before any branch deletion)". On yes, run it (this surfaces a permission prompt — expected), then RE-EVALUATE the gone/merged/ahead-behind/PR classifications against the fresh refs before proposing anything. **On no (or no remote): per rule #9, DISABLE all remote-dependent 🔴 candidates, downgrade their verdicts to provisional, and do NOT delete any branch this run — including under `--auto-safe`.** Local-only operations (reviewing a stash diff, previewing untracked clean) may still proceed with confirmation.

2. **Walk the 🔴 cleanup candidates one at a time.** For each, echo the exact command and the evidence (e.g. "merged into <remote-default> (0 commits ahead of refs/remotes/<R>/<D>, fetched this run)" / "PR #N merged, no post-merge commits, headRef matches" / "upstream [gone], 0 unique commits vs remote default"), then:
   - **`--auto-safe`** auto-runs ONLY when remote-tracking refs were refreshed THIS run, and ONLY: (a) prune of `[gone]` branches that are ALSO fully merged into the remote default, and (b) `git branch -d` of branches merged into the **remote** default. (Each still surfaces a permission prompt the first time per command — that is the second gate; print the command and its result.) If no fetch happened this run, `--auto-safe` auto-runs NOTHING and every candidate falls through to per-item confirmation. It STILL asks before: any `git branch -D`, any stash drop, any `git clean`, any `git worktree remove`, any worktree prune, and any branch whose merge is only local/unverified.
   - **Default mode** confirms EVERY candidate individually before running it.

3. **Never run anything in the Safety-rules forbidden set.** If `git branch -d` refuses, report the refusal and STOP for that branch (offer `-D` only with the full rule-#3 PR-merged proof). For untracked/ignored removal, the `-n` preview must be shown and confirmed per path class. For stashes, show the diff first. For worktrees, only prune dirs already gone from disk, and only after confirming via the per-worktree pass that the worktree had no uncommitted/untracked/unpushed state.

4. **Re-list between destructive ops where indices shift** (e.g. dropping multiple stashes — drop highest index first or re-list).

5. **Re-verify at the end.** Re-run the relevant read-only status lines and reprint so the end state is PROVEN, not assumed.

---

## Final verdict

After the pass, print the summary. ONLY if every condition in Safety rule #11 is verified this run, print exactly:

> ✅ **All clean** — work tree clean (all worktrees), nothing stale/untracked/stashed, every branch pushed with a merged PR, nothing dangling. <If ignored artifacts exist, append: "(N ignored build artifacts present — not evaluated.)">

Otherwise print:

> ⚠️ **Cannot confirm clean** — <list the specific unverified gaps: e.g. "3 commits unpushed on feat/x", "feat/y has no PR", "PR #42 CI failing", "worktree ../sibling is dirty", "gh unavailable — PR state unknown", "no fetch this run — merge verdicts provisional", "2 stashes retained", "detached HEAD with local commits", "1 unpushed tag">.

Never substitute the green line for the amber one.
