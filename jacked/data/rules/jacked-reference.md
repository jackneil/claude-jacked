# Jacked Reference (for Claude Code)

This file gives you deep knowledge about the jacked toolkit installed on this system.
Read this when the user asks about jacked features, installation, logs, or troubleshooting.

## What Jacked Is

- Multi-account manager + skills suite for Claude Code (and Codex): live usage tracking, auto account-switch, smart reviewers, quick commands, web dashboard
- Installed via `uv tool install`, configured via `jacked install`
- Source: https://github.com/jackneil/claude-jacked

## File Locations

| File | Purpose |
|------|---------|
| `~/.claude/settings.json` | Hook configuration (PreToolUse, Stop) |
| `~/.claude/CLAUDE.md` | Behavioral rules (between `# jacked-behaviors-v2` markers) |
| `~/.claude/jacked-reference.md` | This reference doc |
| `~/.claude/agents/*.md` | 10 specialized review/workflow agents |
| `~/.claude/commands/*.md` | Slash commands (20 total; the 7 quick ones detailed under Quick Commands below: /dc, /pr, /learn, /blindspot, /redo, /techdebt, /audit-rules) |
| `~/.claude/skills/*/SKILL.md` | Skills (28 total; every skill is slash-invocable as /name). Ten used to ship as commands: /dcr, /qa, /ux, /qa-video, /demo-video, /docs-sync, /swarm-research, /whats-next, /release, /lockdown. They carry their full engine body in SKILL.md now, so there is no command file for them |
| `~/.claude/jacked-guardrails/*.md` | Guardrails templates (base + 4 languages) |
| `~/.claude/jacked-hooks/*.sh` | Git hook templates (installed extensionless) |
| `~/.claude/jacked-templates/*.html` | HTML scaffolds for human-readable artifacts (plans, specs, research, checkpoints) |
| `<project>/JACKED_GUARDRAILS.md` | Per-project coding standards (created by `jacked guardrails init`) |

## Artifact Format Preference

When you write a file that a **human will open and read** (plans, specs, research summaries, checkpoints, design docs, internal knowledge artifacts), **prefer HTML over Markdown**. Markdown is only a great choice when something else renders it for the human — GitHub's web UI, a wiki engine, a docs site. When the user opens the file directly from disk, HTML wins on every axis:

| Need | Markdown (opened locally) | HTML |
|------|---------------------------|------|
| Headings, sections | Plain text, no styling | Typography, anchored TOC |
| Diagrams | Stays as `mermaid` source code | Renders via Mermaid.js |
| Tables | ASCII pipes | Styled, accessible |
| Code | Backticks | Monospace block with proper background |
| Dark mode | None | `prefers-color-scheme` adapts |
| Print / PDF export | Untyped page breaks | Print stylesheet with `break-inside: avoid` |

### Rule

- **HTML (`.html`)** for: `docs/plans/`, `docs/specs/`, `docs/design/`, `docs/superpowers/plans/`, `docs/superpowers/specs/`, `.claude/checkpoints/`, `.claude/research/`, and any other location holding a human-consumed artifact.
- **Markdown (`.md`)** for: `README.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, `LICENSE.md`, `_wiki/*.md` (GitHub-rendered), and `CLAUDE.md`, `AGENTS.md`, `lessons.md`, `MEMORY.md` (Claude reads these as instructions at session start — Markdown is the format Claude Code expects there).

### How to write an HTML artifact

Start from the bundled template. The canonical filename is **`plan-template.html`** — it covers all artifact types (plans, specs, research, checkpoints) via the `<meta name="jacked:type">` tag and adaptive sections:

```bash
cp ~/.claude/jacked-templates/plan-template.html docs/superpowers/plans/$(date +%Y-%m-%d)-{slug}.html
```

The template includes:
- Embedded CSS (no external stylesheet — works offline)
- Mermaid.js via CDN with **automatic fallback** that surfaces diagram source if the CDN is unreachable
- Dark mode via `prefers-color-scheme`
- Print stylesheet with sensible page breaks
- Metadata `<meta>` tags (`jacked:type`, `jacked:status`, `jacked:branch`, `jacked:date`) so artifacts are machine-introspectable
- Status badges, callouts (info/warn/danger/ok), task checklists, file-structure tables, anchored TOC

Replace `{{PLACEHOLDERS}}`, keep the sections you want, delete the rest. Pure HTML — no preprocessor.

### When to break the rule

You may keep Markdown for an internal artifact **only** when a downstream tool *requires* Markdown input — a static-site generator that ingests `.md`, a linter that scans Markdown for issues, a CI step expecting specific frontmatter. That's the only valid reason.

"It feels short," "no diagrams needed," "it's just notes," or "the user will probably never reopen it" are NOT valid overrides. The template's overhead is one `cp` command; the cost of getting it wrong is a file that's harder to read every time anyone opens it.

## CLI Commands

```
jacked install [--sounds] [--force] [--no-packs] [--packs NAME]  # Install suite (+ default skill packs; --no-packs to skip)
jacked uninstall [--sounds]                         # Remove from Claude Code (also removes enabled skill packs)
jacked packs list                                   # List skill packs + on/off/default status + install counts
jacked packs enable NAME / disable NAME             # Install / durably remove a pack via the npx skills CLI
jacked packs update [NAME]                          # Refresh enabled packs from their upstream repos
jacked skills listing [--apply|--reset] [--window 200k|1m] [--json]  # Skills Claude sees without a description + fix
jacked permissions audit [--fix] [--yes]            # Audit permission rules for dangerous wildcards
jacked check-version                                # Check for newer PyPI version
jacked webux                                        # Launch the web dashboard
jacked service start                                # Start the tray service (menu-bar pill on macOS)
jacked service restart --host 0.0.0.0               # Expose dashboard beyond loopback (Tailscale/LAN)
jacked init [--repo PATH] [--language LANG]          # Set up guardrails + lint hook in project
jacked guardrails init [--repo PATH] [--force]       # Create JACKED_GUARDRAILS.md from templates
jacked lint-hook init [--repo PATH] [--force]        # Install pre-push lint hook in .git/hooks/
jacked dcr engine [--json]                          # Show the /dcr review engine config + Codex readiness
jacked dcr engine set codex --model M --effort E    # Route /dcr volume reviewers to the Codex CLI
jacked dcr engine clear                             # Back to Claude reviewers (the default)
jacked tests run [--kind full|targeted] -- CMD...   # Run a test gate, record the run, return its exit code
jacked tests seen -- CMD...                         # Exit 0 when this tree passed this gate command here
jacked tests profile / history / loss               # Gate time, run history, tests a change removes
python -m jacked                                    # Alternative invocation
```

DCR review engine (0.91.0+): `/dcr` can run its volume reviewers on the OpenAI
Codex CLI instead of Claude Task subagents, so review fan-out spends the user's
OpenAI subscription instead of their Anthropic plan. Config lives in
`~/.claude/jacked-dcr.json`, written by `jacked dcr engine set` or the dashboard
card (Settings > Features > Review Engine; GET/PUT /api/dcr-engine). At review
time /dcr runs one `jacked dcr engine --json` call; the output carries
engine/model/effort, the `keep_on_claude` lens list, a live Codex preflight
(installed + signed in = `usable`), and the packaged findings-schema path
(`jacked/data/schemas/dcr-review-output.schema.json`) that Codex jobs bind with
`--output-schema`. Judgment never moves: lens selection, finding validation, the
fix phase, and the verdict stay in the parent Claude session; `keep_on_claude`
lenses (default Security + Frontend Design) always spawn as Claude reviewers.
Every failure shape falls back to Claude - codex missing/unauthed, a corrupt
config file, a failed or hung Codex job (that one reviewer re-runs as a Claude
Task) - so a review never blocks on the engine and no lens is ever dropped.
Defaults when enabling: model `gpt-6-astra`, effort `xhigh`. Any Codex model
name is accepted; effort is one of none/minimal/low/medium/high/xhigh/max/ultra.
`gpt-6-astra` needs Codex CLI 0.153.0 or newer; on an older CLI the engine
reports the reason and reviews on `gpt-5.6-sol` until you run `codex update`.
A stored `gpt-5.6-luna` (the default through 0.103.0) migrates to `gpt-6-astra`.

Chain-of-command auto-load (0.76.0+): `jacked install` registers a synchronous
SessionStart hook (`jacked _hook chain_of_command_context`) that injects the
chain-of-command dispatch policy (from ~/.claude/skills/chain-of-command/SKILL.md)
into every new Claude Code session, making the model-dispatch lanes binding from
the first turn with no /chain-of-command invocation. Disable: toggle the
chain-of-command skill off in the dashboard (hook goes silent when the skill
file is absent). `jacked uninstall` removes the hook entry.

Skill packs (0.82.0+): curated collections of third-party skills installed LIVE
from their upstream GitHub repos via the vercel-labs skills CLI (`npx skills`,
Node 18+ required); nothing is vendored. Registry ships in jacked/data/packs.json
(currently `marketing` = 28 curated skills from coreyhaines31/marketingskills,
`design-extras` = improve-animations from emilkowalski/skills). Packs marked
`default: true` install by DEFAULT on a plain `jacked install`; opt out with
`jacked install --no-packs` (per-run) or `jacked packs disable NAME` (durable).
State in ~/.claude/jacked-packs.json is a v2 three-state model
(`{"version":2,"packs":{name:{"state":"enabled"|"disabled","at":iso}}}`): an
explicit decision wins, and a pack with no entry follows the registry `default`.
Disable writes a durable "disabled" (never drops the entry) so a default-on pack
the user removed is not silently reinstalled next install. v1 files migrate on
load. Skills land canonical in ~/.agents/skills/ with symlinks in ~/.claude/skills/
tracked by the skills CLI lockfile (~/.agents/.skill-lock.json). Every
`jacked install` refreshes the effectively-enabled packs (defaults minus
disabled, plus explicit-enabled). Dashboard toggles: Settings > Features > Skill
Packs (GET/PUT /api/packs; GET exposes enabled/default/explicit per pack). Exit
codes from the skills CLI are untrusted (rc=0 even when nothing installs); jacked
verifies every operation on disk. Removal is lockfile-source-checked and install
refuses to overwrite a skill dir you already own (both from another source or
unmanaged) so a same-named user skill is never clobbered.

Remote dashboard access (0.76.0+): the dashboard binds 127.0.0.1 by default. Any
`--host` beyond loopback is hardened server-side (no CORS wildcard, same-origin
WebSocket gate, cross-site write rejection, Host-header validation against DNS
rebinding); there is still NO auth layer, so reachability must be restricted at
the network layer (Tailscale ACL scoped to the port, firewall). Escape hatches:
`JACKED_ALLOWED_ORIGINS` (extra cross-origin consumers) and `JACKED_ALLOWED_HOSTS`
(custom DNS names) env vars on the service process. Alternative that needs no
rebind: `tailscale serve --bg 8321`. See README "Remote Access (Tailscale)".

Retired in 0.70.0: the security gatekeeper (`jacked gatekeeper *`, superseded by
Claude Code's native auto permission mode) and Qdrant session search
(`jacked search/backfill/status/configure`, the `/jacked` skill, the `[search]`
extra). `jacked install` prunes their hooks from settings.json automatically.

## Gate recorder (jacked tests)

The `jacked tests` group records each run of a test gate. It reads the JUnit XML report of the runner: pytest, vitest, jest, Playwright, Go (gotestsum) or cargo-nextest. Then it tells you if a tree passed a gate command, where the time of a gate goes, and which tests a change removes or weakens.

### The gate line

Put this block in the gate script of a repository. When the installed jacked has no `tests` group, the block runs the bare gate command and prints one line on standard error. The exit code is always the exit code of the gate command.

<!-- gate-line -->
```sh
if jacked tests run --help >/dev/null 2>&1; then
  jacked tests run --kind full -- uv run python -m pytest -n auto --junitxml={junit}
else
  echo "jacked tests is not installed: the gate runs with no record" >&2
  uv run python -m pytest -n auto
fi
```

### Commands

```
jacked tests run [--kind full|targeted] [--junit GLOB] [--input GLOB] [--label TEXT] [--runner NAME] [--strict] -- CMD...
jacked tests record --junit PATH [--exit-code N] [--wall SECONDS] [--commit SHA] [--machine LABEL] [--report-root PATH] [--started ISO] [--kind full|targeted] [--label TEXT] [--runner NAME]
jacked tests seen [--input GLOB] [--any-machine] [--json] -- CMD...
jacked tests profile [--run ID] [--machine LABEL] [--top N] [--json]
jacked tests history [--machine LABEL] [--days N] [--size] [--json]
jacked tests prune --before YYYY-MM-DD
jacked tests loss [--base REF] [--json]
```

- `jacked tests run` runs CMD, records the run, and returns the exit code of CMD. A recorder error never changes that code. The one exception is `--strict`: it gives exit 1 when the gate returns 0 and the report holds failures. When CMD cannot start, the exit code is 127. When signal N stops CMD, the exit code is 128 plus N. A SIGTERM or a SIGHUP to `run` goes on to CMD. Under `nohup`, SIGHUP stays ignored for CMD, as with `nohup CMD`. Put the options before CMD. Put `--` between the options and CMD.
  - The placeholder `{junit}`: an argument that holds the text `{junit}` gets the path of a private report file. `run` makes a private temporary folder for it, reads the report after the gate, and then removes the folder. The record holds the SHA-256 of the arguments as you typed them, so the path does not change the identity of the command.
  - A command with no `{junit}` and a run with no `--junit` have no report. Then `run` prints one line before the gate: the run cannot prove a tree.
  - After each record, `run` prints one line: `recorded <run id>, verdict <verdict>.`
  - When `run` cannot write the history store, it prints one line that names the store folder, and it does not record the run. The exit code is still the exit code of the gate. A sandbox that makes the `.git` folder read-only gives this result, for example the default sandbox of Codex.
  - `--junit GLOB`: use it for a runner that writes its report at a fixed path. Before the gate, `run` takes the SHA-256 of each file that matches. After the gate, it reads each match that is new or that has new content. It prints one line for each match that it did not read, so an old report never becomes this run.
  - `--kind full|targeted` is a label for `profile` and `history` only.
  - The verdict of a run is one of five values. `passed`: exit code 0, a report with at least one test, and no failed or error test. `failed`: an exit code that is not 0. `conflict`: exit code 0 and a failed or an error test in the report. `no-data`: exit code 0 and no test data. `unknown`: no exit code. A pipe can turn a red gate into exit code 0, and then the verdict is `conflict`.
  - When git cannot read the work tree (for example, a file that you cannot read, or a clean filter that git cannot start), `run` prints one line with the first error line of git, and the run cannot prove a tree. `seen` prints the same line. A repository with no commit gives a line too.
  - A gate can change a path that git sees, for example a cache file that git does not ignore. Then `run` prints each changed path, and the run cannot prove a tree. Add these paths to `.gitignore`.
  - A red run whose failing set is in an earlier record prints a notice. The notice gives the number of earlier records, the number of trees, and how many of the records are on the default branch. A count that some records do not hold prints as in `history`: `K (M not recorded)`, or `not recorded (N runs)`. Then it gives the failing test IDs and one line for each earlier record, by machine. It does not say whose defect it is.
  - Outside a git repository, the gate runs and jacked records nothing. The line says which git failure it is: no git program, a git call that does not answer, a repository that another user owns (git names `safe.directory`), or a folder outside a work tree. The line holds the first error line of git. The other commands print the same line and give exit 1.
  - Each output line of `jacked tests` is one line. A line break or a bidi control in a stored value (a test ID, a file name, the text of an error) prints as an escape, for example `\n` or `\u2028`.
- `jacked tests record` records the JUnit files of a gate run that `run` did not wrap, for example a run on hosted CI. Without `--exit-code`, the verdict is `unknown`. `--machine LABEL` names the machine that ran the gate. `--report-root PATH` gives the root folder of the machine that wrote the report, so the reader can make the paths relative. `--started ISO` needs a time zone. The command prints `<run id> <verdict>`.
- `jacked tests seen` looks for a wrapped run on this machine that proves that this tree passed this command. When it finds one, it prints one line about the run and gives exit 0. When not, it prints one line and gives exit 1.
  - A run proves it when all of these are true: `run` made it, its verdict is `passed`, and the gate did not change the tree. The tree hash, the command SHA-256 and the `--input` fingerprint must also be equal.
  - The newest run of the same tree, command, folder, inputs and machine decides. When that run did not pass, `seen` gives exit 1, and its line names that run and the cause. An older pass never hides a newer failure.
  - The command identity holds the folder that the gate ran in, relative to the top of the work tree. A run in `sub` never proves the same command at the top, because the same command can collect a different set of tests in a different folder. Run `seen` in the folder where the gate runs. The hit line names that folder.
  - A record of an older or newer record format never proves a tree. `history` and `profile` still show it.
  - The commit is not compared, so two commits with the same content are one tree. A run of `record` never proves a tree.
  - When this machine has no identity (the file cannot be read or made, or holds no valid identity), `seen` prints one line that names the file and the cause.
  - `--any-machine` also accepts a run of a different machine. The newest run of each machine decides for that machine. `seen` never compares the times of two machines. `--json` prints the record, or `null`.
- `jacked tests profile` shows where the time of a run goes, by file and by test.
  - By default, it shows the newest run with the kind full. When no run has the kind full, it shows the newest run. The default machine is the machine of the newest full run.
  - It prints the number of files that hold half of the test seconds. When each file took 0 seconds, it prints `every file took 0 seconds`. When some files have no time and each other file took 0 seconds, it prints `every file with a time took 0 seconds`.
  - A value that the record does not hold prints as text (`no time recorded`, `no time`, `kind not given`), never as 0 or as an empty space. The JSON gives `null`.
  - `--top N` sets the rows of each text table, and `--json` prints all rows. It prints a setup share only when the record holds one, and a JUnit report never holds one.
  - With no run in the history, `profile` gives exit 1. An unknown `--run` or `--machine` also gives exit 1.
- `jacked tests history` shows the runs of one machine.
  - It shows the verdict counts, the failing sets and the flaky tests. It also shows the trees that passed one command two times or more, and the labels of the other machines.
  - `--days N` shows only the last N days. `--size` adds the size of the history in bytes.
  - A value that the record does not hold prints as text, never as 0. A count of a failing set that no run holds prints `not recorded (N runs)`. A count that only some runs hold prints `K (M not recorded)`. A size that jacked cannot read prints `the folder cannot be read`.
  - With no run in the history, `history` prints one line and gives exit 0. An unknown `--machine` gives exit 1.
- `jacked tests prune --before YYYY-MM-DD` deletes each run whose UTC date is before that date. It prints `deleted <run id>` for each deleted run. jacked deletes nothing automatically.
- `jacked tests loss` compares the work tree with the merge base of HEAD and `--base REF`. The work tree includes the commits, the changes that are not committed, and the untracked files that git does not ignore. With no `--base`, REF is `refs/remotes/origin/HEAD`. `loss` does no fetch. It is a report, and it gives exit 0 for each result.
  - The output lines are `base <commit> (<ref>)`, `net <+N|-N|0>` and `added N, removed N`. Then come the lines `removed <test>`, `added <test>`, `moved <old> -> <new>`, `fewer assertions <test>`, `fewer cases <test> <before> -> <after>`, `skip mark added <test>`, `not read <test> (<reason>)` and `not read file <path> (<kind>)`.
  - A renamed file, a test that moves to a different file, and an edited parameter value give no loss. A test that gets a new name inside one file shows as one removed test and one added test.
  - `loss` counts these cases: the literal lists of Python parametrize marks, the rows of a literal `.each` table (TypeScript and JavaScript), and the `t.Run` calls of a Go test.
  - A `t.Run` in a for-range loop over a slice literal counts each element of that literal. The literal must be inline, or assigned in the same function and not assigned again. This Go rule reads gofmt spacing.
  - `loss` does not count a computed case list. It also does not count a `t.Run` in a loop over any other value, or a fixture parameter.
  - When a paired test has a computed case list, or a body that `loss` cannot read, `loss` prints a `not read` line.
  - A TypeScript, JavaScript, Go or Rust file with a comment or a string that does not close gives a `not read file` line with the line number, because `loss` cannot be sure of the rest of that file. A regex literal (for example `/\/*$/`) is not a comment.
  - When `loss` cannot make a report, it prints one line and gives exit 1. With no `refs/remotes/origin/HEAD`, give `--base`, or run `git remote set-head origin --auto`. In a shallow clone with no merge base, run `git fetch --unshallow`. On a branch with no commit, make a commit first.

### Where the history is

The history is in `<git common dir>/jacked-tests/runs/`. To find the common git directory, run `git rev-parse --git-common-dir`. All linked worktrees of one clone use the same history. The history is inside the git directory, so git never commits it.

Each run has two files: `<id>.tests.json.gz` and `<id>.meta.json`. The files hold test IDs, numbers, states, the class name of a failure, and the SHA-256 of the command. They hold no failure message, no test output and no command text.

To delete runs, run `jacked tests prune --before YYYY-MM-DD`. To delete all of the history, delete the `jacked-tests` folder in the common git directory.

A machine has a label: `local` for a run of `run`, or the value of `record --machine`. A machine also has a private random identity in `~/.claude/jacked-tests-machine-id` (under `JACKED_HOME` when it is set). No command prints the identity. One label can have two identities, for example a host and a container that open one clone. Then `history` and `profile` say so.

### Limits of seen

- The tree identity does not hold a file that git ignores. When the gate reads such a file, give it with `run --input GLOB`. Then give the same globs with `seen --input GLOB`.
- The `--input` fingerprint holds the path, the size and the modification time in nanoseconds of each file. It does not hold the content. A glob that matches a folder adds each regular file below that folder, hidden files too. It does not follow a symbolic link. A glob that matches no regular file gives one line in `run` and in `seen`.
- The tree identity does not hold a change inside the work tree of a submodule.
- An assume-unchanged bit, `core.ignoreStat` and a skip-worktree bit do not hide an edit from the tree identity. jacked clears these bits in its own copy of the index, and never in the real index. In a sparse checkout, the skip-worktree bits stay: a tracked file that the cone leaves out stays as HEAD has it.
- With git-lfs, the clean filter can write a copy of a new large file into `.git/lfs/objects` of the clone. The copy uses disk space, and no commit uses it. To remove such copies, run `git lfs prune`.
- On Windows, an edit that keeps the size and puts back the old modification time is not seen. `git status` does not see it either.

### A red gate

When the gate is red, read the failing tests. When the same tests fail on the base branch, repair the checkout or the base. Then run the gate again. A failure on the base branch never clears the branch.

### Runner settings

- pytest: give `--junitxml={junit}`.
- vitest: give `--reporter=junit --outputFile.junit={junit}`.
- gotestsum: give `--junitfile {junit}`.
- Playwright, cargo-nextest and jest write the report at the path that their configuration gives. Give that path with `run --junit GLOB`.
- jest: the reader needs four jest-junit settings. In the environment, set `JEST_JUNIT_ADD_FILE_ATTRIBUTE="true"`, `JEST_JUNIT_CLASSNAME="{classname}"`, `JEST_JUNIT_TITLE="{title}"` and `JEST_JUNIT_ANCESTOR_SEPARATOR=" > "`. In the jest-junit configuration, the same settings are `addFileAttribute: "true"`, `classNameTemplate: "{classname}"`, `titleTemplate: "{title}"` and `ancestorSeparator: " > "`. With the default templates, the test ID holds the describe titles two times.

## Codex Integration

When Codex is present (the `codex` binary is on PATH, or a `~/.codex` home exists),
`jacked install` runs a second pass that deploys jacked into Codex's native formats
alongside the Claude Code install. It is idempotent, tracked by its own manifest,
and fully reversed by `jacked uninstall`.

What ships to Codex:

| jacked artifact | Codex target | Notes |
|------|------|------|
| Skills | `~/.agents/skills/<name>/` | Full directory copy (sidecar files included), the agentskills.io format Codex discovers. |
| Commands | `~/.codex/prompts/<name>.md` AND `~/.agents/skills/<stem>/SKILL.md` | OpenAI deprecated `~/.codex/prompts` on 2026-01-22 in favor of skills, so each non-excluded command also ships as a command-derived skill; the prompts copy stays for back-compat during the deprecation window. This applies to the command files only. The ten migrated names (`dcr`, `qa`, `ux`, `qa-video`, `demo-video`, `docs-sync`, `swarm-research`, `whats-next`, `release`, `lockdown`) are skills now and ship their full content straight across, with their `$name` prompts generated from the skill. |
| Rules | managed block in `~/.codex/AGENTS.md` | The Claude-authored behaviors, adapted for Codex: `CLAUDE.md` references rewritten to `AGENTS.md`, plus a "Codex runtime adapter" section that maps Claude vocabulary to Codex equivalents (slash commands to `$skill` invocations, the Task/Agent tool to native subagents, Claude model lanes ignored, `mcp__chrome-devtools__*` names, plan mode). |
| Review agents | `~/.codex/agents/<name>.toml` | The 10 review agents converted to Codex custom-agent TOML (`name`, `description`, `developer_instructions`), with no model pin so Codex picks its own. |
| chrome-devtools MCP | jacked-managed marker block in `~/.codex/config.toml` | Registers the same npx server the Claude side uses, so Codex skills referencing `mcp__chrome-devtools__*` resolve. A user's own chrome-devtools entry is never touched, and a write that would break the TOML is rolled back. |
| QA-suggest hook | `~/.codex/hooks.json` (Stop event) | The runtime-portable `qa_suggest` hook with `--runtime codex`, so the suggestion reads `$qa` (the Codex skill invocation) instead of `/qa`. Codex requires a one-time trust for this hook: run `/hooks` inside Codex to approve it (the installer prints this reminder when the entry is newly added). |

Claude-only artifacts excluded from Codex: the `chain-of-command` and `recover`
skills, and the `swarm`, `goal-maker`, `browser-reset`, and `jacked-setup`
commands (each is wired to Claude Code machinery Codex has no analog for).
Upgrades prune any previously-shipped copies of these.

Manifest and uninstall: everything above is recorded in
`~/.codex/jacked-codex-manifest.json` (skills, prompts, agents, mcp, hooks), which
keeps the Codex install idempotent and lets `jacked uninstall` remove exactly what
jacked wrote, never a user's own entries. The Claude manifest
(`~/.claude/jacked-manifest.json`) is separate and untouched.

## Guardrails System

Language-specific coding standards enforced through templates and git hooks.

**Templates** (`~/.claude/jacked-guardrails/`):
- `base.md` — universal rules: size limits, structure, /dc before commits, lint before push
- `python.md`, `node.md`, `rust.md`, `go.md` — language-specific tooling and patterns

**Per-project setup** (`jacked init` or `jacked guardrails init`):
- Auto-detects language from pyproject.toml/package.json/Cargo.toml/go.mod
- Creates `JACKED_GUARDRAILS.md` in project root (base + language template)
- Claude follows these because global CLAUDE.md says "follow JACKED_GUARDRAILS.md or DESIGN_GUARDRAILS.md if they exist"

**Git pre-push hook** (`jacked lint-hook init`):
- Installs to `.git/hooks/pre-push` (extensionless, as git requires)
- Runs language-appropriate linter before allowing push
- Detects existing hook frameworks (husky, pre-commit, lefthook) and warns

**Dashboard warnings**:
- Projects with recorded activity but no JACKED_GUARDRAILS.md show "No Guardrails" badge
- Projects without our pre-push hook show "No Lint Hook" badge
- One-click setup from dashboard creates guardrails and/or installs hooks

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Hook not running your code changes | Check `~/.claude/settings.json` hook path -- may point to stale uv/pip install instead of current env |
| "jacked: command not found" | Run `uv tool update-shell` and restart terminal |
| Dangerous permission wildcards | Run `jacked permissions audit --fix` to find and prune them |
| Service will not start | Run `jacked service status`. It prints the last start failure with its reason. The statusline shows `jacked service down` while the service is not running. Correct the cause, then run `jacked service restart` |
| Upgrade left the service down | Read `~/.claude/jacked-update-failed.txt`. `jacked upgrade` is a transaction: it runs `jacked service preflight` before it replaces the old install, and it reinstalls the previous version if the new one cannot start. The file names both versions and the manual recovery commands |
| Is this build able to start? | Run `jacked service preflight`. Exit code 0 means the build can provision its service contract. Exit code 1 prints `[FAIL] <error type>: <reason>`. The command starts no process and changes no supervisor. Add `--json` for one machine-readable object |

## Load Budget

At session start, Claude Code loads only the first 200 lines or the first 25 KB of the auto memory index, `~/.claude/projects/<project>/memory/MEMORY.md`. The rest of the file does not load, and Claude Code gives the user no sign of it. Each CLAUDE.md loads in full up to 4 MiB. The Claude Code docs advise under 200 lines for each CLAUDE.md.

jacked measures these files and warns when a file is over 90% of its cap. It never edits a file. The warning shows in three places:

- At session start, after the git reality block and before the integrity block, which is the last block of the SessionStart output. A session with no warning gets no extra text.
- In `jacked memory status`, after the other output.
- On the dashboard, on the Memory Vault card (Settings > Features). The card shows the warning when the vault is off too.

To groom MEMORY.md, run the memory-librarian skill. To audit a CLAUDE.md, run `/audit-rules`. The caps are constants in `jacked/memory/load_budget.py`.

## Compaction

Claude Code compacts a long conversation into a summary. It compacts automatically near the context limit, and when you run `/compact`. A summary can lose the state of the work in progress. After a compaction, the project-root CLAUDE.md and the auto memory index load again from disk, but instructions that were only in the conversation do not.

To control what the summary keeps, run `/compact <instructions>`. Keep this work state each time:

| Keep | Why |
|------|-----|
| The modified files | The next step must know which files changed and which edits are not tested |
| The test commands and their last results | The next run must use the same commands and compare against the last result |
| The current task and its next step | The session must continue the same task, not start again |
| The punted items and the reason for each | Work that was put off must not disappear in the summary |

Example:

```
/compact Keep the work state: the modified files, the test commands and their last results, the current task and its next step, and each punted item with its reason.
```

For instructions that apply to every compaction, add a "Compact Instructions" section to CLAUDE.md. The jacked behavioral rules contain one line that asks for this work state at each compaction.

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `JACKED_HOST` / `JACKED_PORT` | 127.0.0.1 / 8321 | Dashboard/service bind address |

## Quick Commands

| Command | What It Does |
|---------|-------------|
| `/dc` | Double-check reviewer -- auto-detects phase (planning/implementation/post-implementation) |
| `/pr` | Pull request workflow -- checks status, creates/updates PRs |
| `/learn` | Distills a lesson from the current session into a CLAUDE.md rule |
| `/blindspot` | Pre-build discovery pass: surfaces unknown-unknowns, teaches you to prompt well, asks must-have questions |
| `/redo` | Scraps current approach, preserves work, re-implements with hindsight |
| `/techdebt` | Scans for TODOs, oversized files, missing tests, dead code |
| `/audit-rules` | Audits CLAUDE.md for duplicates, contradictions, stale rules |

## Smart Reviewers (10 Agents)

| Agent | Focus |
|-------|-------|
| double-check-reviewer | Security, auth, RBAC, org isolation, architecture |
| code-simplicity-reviewer | Over-engineering, unnecessary abstractions |
| defensive-error-handler | Missing error handling, potential crashes |
| test-coverage-engineer | Test gaps, coverage analysis |
| test-coverage-improver | Adds doctests and test files |
| git-pr-workflow-manager | Branch management, PR organization |
| pr-workflow-checker | PR status and lifecycle |
| issue-pr-coordinator | Issue grouping, PR-issue linking |
| readme-maintainer | README sync with code changes |
| wiki-documentation-architect | GitHub Wiki maintenance |
