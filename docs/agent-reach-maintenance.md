# Agent-Reach pin maintenance

This is the maintainer runbook for keeping jacked's vendored Agent-Reach trust
anchor current. Agent-Reach is jacked's second managed external integration (the
first is Chrome DevTools MCP). jacked never installs Agent-Reach from a moving
target: it installs exactly the commit SHA, the fully-pinned transitive
constraints, the hash-verified skill payload, and the pinned channel-backend
versions recorded in two vendored files:

- `jacked/data/integrations/agent-reach.json` — the pin (SHA, skill hashes, channel version table, upstream URL, version label, vetted date)
- `jacked/data/integrations/agent-reach-constraints.txt` — the locked transitive dependency set

Both files ship inside the wheel and are the anchor the runner
(`jacked/integrations/agent_reach.py`) and the pin loader
(`jacked/integrations/pinfile.py`) trust. Bumping the pin is a **security
review**, not a routine dependency update. Treat it like one.

## When to bump

Bump the pin only when one of these is true:

1. **Upstream platform fix needed.** A channel broke upstream (platform
   whack-a-mole, a `yt-dlp` bump, a login-flow change) and the fix landed on
   upstream `main`. Because the constraints file pins transitive deps, upstream's
   dep-level fixes wait for a pin bump. This is the accepted cost of the V2
   mitigation (poisoned transitive dependency); break-glass keeps it survivable
   for users who cannot wait.
2. **Scheduled refresh.** Periodic re-vet to pull upstream security fixes and
   keep the channel version table from drifting far behind.
3. **A user break-glass report.** A user hit a break-glass override
   (`jacked reach update --override-ref <ref> --unvetted-ok`) to unblock
   themselves. Their override is running **unvetted** upstream code. Vet the ref
   they used, and if it is clean, ship it as the new pin so their override
   auto-clears (see "Break-glass reality" below).

## Procedure

Run the vetting pipeline against the ref you want to vet. Default is upstream
`main`; pass an explicit SHA or tag to vet a specific point.

```bash
uv run python scripts/vet_agent_reach.py --ref main
```

`scripts/vet_agent_reach.py` is a repo-side tool (it is **not** shipped in the
wheel). It never installs or runs Agent-Reach. In one pass it:

1. **Resolves the ref to a full 40-char commit SHA** (`git ls-remote`). A pin is
   always an immutable SHA, never a tag or branch — tags are mutable and are the
   V1 poison vector.
2. **Shallow-fetches the tree at that SHA** into the work dir and reads the
   upstream `[project].version` as the version label.
3. **Diffs against the current pin** — changed files, added/removed/changed
   dependencies, and risk-pattern hits in the changed files (subprocess,
   `os.system`, `requests.`, `urllib`, and similar network/exec heuristics). If
   the prior pinned SHA cannot be fetched for a diff, the report says so and you
   review the full tree manually.
4. **Compiles `agent-reach-constraints.txt`** from upstream's `pyproject.toml`
   via `uv pip compile --universal` (a fully-pinned, cross-platform locked set).
5. **Audits the locked set** with `uvx pip-audit`. Findings never fail the run —
   they are captured verbatim in the report and the human review gate decides.
6. **Re-hashes the skill payload** — sha256 of `SKILL.md`, `SKILL_en.md`, and
   every `references/*.md` in upstream's skill dir.
7. **Resolves exact channel-backend versions** (`npm view <pkg> version` for npm
   backends, the PyPI JSON API for pipx backends) and rebuilds the channel
   version table, pinning each backend to an exact version.
8. **Builds + validates the pin**, then writes the two vendored files plus a
   human-readable vetting report.

### Outputs

| File | Location |
|------|----------|
| Pin | `jacked/data/integrations/agent-reach.json` |
| Constraints | `jacked/data/integrations/agent-reach-constraints.txt` |
| Vetting report | `<work-dir>/agent-reach-vetting-report.md` (scratch; not committed) |

Useful flags:

- `--ref <branch|tag|SHA>` — what to vet (default `main`).
- `--exclude-newer <YYYY-MM-DD>` — refuse deps published after the given day
  when compiling constraints, matching jacked's smash-and-grab defense. Off by
  default; use it for a reproducible, time-bounded compile.
- `--work-dir` / `--report-dir` / `--output-dir` / `--vetted-at` — override the
  scratch clone location, the report location, the pin output dir, and the
  recorded vetted date (default: today).

### Review the diff like a security review

This review **is the entire point of the pin**. Do not rubber-stamp it. Read the
printed vetting report and the git diff of the two regenerated vendored files,
and pay attention to:

- **New or changed dependencies** in `agent-reach-constraints.txt`. A new
  transitive dep is a new trust decision. Cross-check the pip-audit output.
- **Changed install code** upstream. The risk-pattern hits point you at the
  files worth reading — new subprocess/network calls in the installer are how a
  poisoned release reaches the machine. Confirm upstream still honors `--safe`
  (jacked always installs with `--safe`, which suppresses the `curl|bash`
  NodeSource setup, apt keyring writes, and unprompted global npm installs).
- **Skill-content changes** (`SKILL.md` / `SKILL_en.md` / `references/*.md`). A
  changed skill hash means the prompt-injection surface changed. Read the
  upstream skill diff and confirm the new content is not adversarial — this is
  the V5 vector (a malicious `SKILL.md` persisting in agents' skill dirs).
- **Channel version jumps.** A backend version that jumped several majors is
  worth a look before you pin it (V3: poisoned channel backend).

### Commit and ship

Commit **only** the two regenerated files:

```bash
git add jacked/data/integrations/agent-reach.json \
        jacked/data/integrations/agent-reach-constraints.txt
```

Open a PR referencing the vetting report findings (SHA, version label,
pip-audit verdict, notable diff/skill/channel changes). The pin ships with the
next jacked release; users pick it up on upgrade.

## Locale mapping gotcha (skill hashes)

The pin records **source-tree** hashes, and it hashes **both** `SKILL.md` and
`SKILL_en.md`. Upstream's installer does not install both: `_install_skill`
picks the locale winner (`SKILL.md` or `SKILL_en.md`) and writes it **as**
`SKILL.md` in the target dir. No file named `SKILL_en.md` is ever installed.

`installed_layout` in `jacked/integrations/_util.py` bridges this: the installed
`SKILL.md` must match **either** source hash, and every other pin entry
(`references/*.md`) must match its own hash by name. So when you regenerate the
pin, expect both `SKILL.md` and `SKILL_en.md` hashes to be present — that is
correct, not a duplicate. Do not hand-strip one; the verifier depends on both.

## Break-glass reality

Users can override the shipped pin to any upstream ref:

```bash
jacked reach update --override-ref <ref> --unvetted-ok
```

The runner still resolves the ref to a full SHA and compiles fresh, fully-pinned
constraints for it — so the override is still SHA-locked and constraint-locked,
just **unvetted** (jacked did not review it). The override state lives in the DB
`settings` table (`reach_override_sha`, `reach_override_ack`, `reach_override_at`,
all in `_PROTECTED_SETTING_KEYS` so the generic settings endpoint cannot rewrite
them). While an override is active, `jacked reach status` and the dashboard card
render an **UNVETTED** badge.

An override **auto-clears when the shipped pin reaches that SHA**. So the fix for
a user stuck on a break-glass override is to vet their ref and ship it as the
pin: on their next upgrade the runner sees `override_sha == pin.commit_sha`,
clears the override, and reinstalls at the (now vetted) shipped pin. Detection is
offline and only catches the equality case; true ancestry ("the pin advanced
past the overridden SHA") is not detected without fetching git history.

## Chrome DevTools MCP pin

The other managed integration is pinned the same way, but its pin is a single
constant, not a vendored file. Bump `CHROME_DEVTOOLS_NPX_PACKAGE` in
`jacked/cli.py` (currently `chrome-devtools-mcp@<version>`; `CHROME_DEVTOOLS_MCP_PACKAGE`
holds the bare package name that the Codex installer imports).

Vet a bump the same "read it like a security review" way:

```bash
npm view chrome-devtools-mcp version        # latest published version
npm view chrome-devtools-mcp                 # metadata, repo, maintainers
```

Read the upstream changelog for the version range you are jumping, confirm no
new install-time execution or permissions surface, then update the constant and
commit. Like Agent-Reach, it ships pinned to an exact version so a poisoned
`@latest` cannot reach users.
