Introducing claude-jacked: Stop Babysitting Claude Code

Paste this into Claude Code. Pick your tier. Everything changes:

Install claude-jacked for me. Use AskUserQuestion to ask me which features I want:
1. First check if pipx and jacked are already installed
2. Ask me which install tier I want:
   - BASE: Smart reviewers, commands (/dc, /pr, /learn, etc.), behavioral rules
   - SEARCH: Everything above + session search across machines (requires Qdrant Cloud)
   - SECURITY: Everything above + auto-approve safe bash commands (fewer permission prompts)
   - ALL: Everything
3. Install based on my choice:
   - BASE: pipx install claude-jacked && jacked install --force
   - SEARCH: pipx install "claude-jacked[search]" && jacked install --force
   - SECURITY: pipx install "claude-jacked[security]" && jacked install --force --security
   - ALL: pipx install "claude-jacked[all]" && jacked install --force --security
4. If I chose SEARCH or ALL, help me set up Qdrant Cloud credentials
5. Verify with: jacked --help

https://github.com/jackneil/claude-jacked

Approving "git status" for the 50th time? Auto-approved in 2ms.
Shipping unreviewed code? 10 smart reviewers auto-trigger.
Lost context between machines? Search past sessions by meaning.
Same mistake twice? /learn writes permanent rules from corrections.
Claude codes before thinking? Forced plan-first workflow.
Teammate solved this last week? Search their sessions.
PR workflow sucks? /pr does it all in one command.
Wrong approach, patching patches? /redo stashes and rewrites clean.
Tech debt invisible? /techdebt finds it automatically.
CLAUDE.md is a mess? /audit-rules cleans it up.
No alerts when Claude needs you? Cross-platform sound notifications.

Here's how the big ones work.


THE SECURITY GATEKEEPER

Every bash command Claude runs goes through a 4-tier evaluation chain, fastest first:

Tier 1, Deny patterns (under 1ms): Hard-coded regex catches catastrophic stuff. sudo, rm -rf /, disk wipes, credential exfiltration, reverse shells. Never auto-approved.

Tier 2, Permission rules (under 1ms): Checks commands you've already permanently approved in your Claude settings.

Tier 3, Local allowlist (under 1ms): Pattern matching against known-safe commands. git, pytest, npm test, linting, docker, read-only inspection. 90% of commands match here.

Tier 4, LLM evaluation (~2 seconds): Sends ambiguous commands to Claude Haiku. If the command references a Python, SQL, or shell script, the gatekeeper reads the file contents and sends them along. Haiku evaluates what the code actually does, not just the command name.

If all tiers fail? Falls back to the normal Claude permission prompt. Fail-open, never locked out.

Auto-approved instantly:
  git status - safe pattern, <2ms
  pytest -x tests/ - safe pattern, <2ms
  python -m pytest - safe module, <2ms
  python run_tests.py - LLM reads the file, sees only test code, ~2s

Asks you first:
  rm -rf ~/ - hard deny, instant block
  cat ~/.ssh/id_rsa - credential access, instant block
  python cleanup.py - LLM reads the file, sees shutil.rmtree, flags it


SLASH COMMANDS

/dc - Double-check reviewer. Reviews your recent work for security holes, logic errors, and complexity. Auto-detects planning vs implementation vs post-implementation phase.

/pr - PR workflow. Checks branch state, creates or updates pull requests with proper issue linking.

/learn - Distills lessons into permanent CLAUDE.md rules. Three failures on the same concept and it graduates automatically.

/redo - Creates a safety branch, stashes your work, forces structured reflection, rewrites clean.

/techdebt - Scans for TODOs, oversized files, missing tests, dead code, stale imports.

/audit-rules - Finds duplicates, contradictions, stale rules, and vague directives in CLAUDE.md.

Plus 10 specialized reviewer agents (code simplicity, error handling, test coverage, PR workflow, wiki docs, readme maintenance, and more) that Claude invokes automatically.


BEHAVIORAL RULES

Plan first: Claude enters plan mode for non-trivial tasks before writing code.
Verify before done: Runs tests and proves correctness before marking work complete.
Learn from corrections: Checks lessons.md and writes sharper lessons on repeat mistakes.
Auto-trigger reviewers: Suggests /dc before shipping, /techdebt during long sessions, /redo when patches stack.


SESSION SEARCH (OPTIONAL)

Every session auto-indexes to Qdrant Cloud. Search by meaning:

/jacked that authentication bug I fixed last week
/jacked how did Sam implement the payment system
/jacked shopping cart feature I was building on my desktop

Cross-machine sync, team sharing built in. Start on desktop, continue on laptop.


OPEN SOURCE

MIT licensed.
PyPI: https://pypi.org/project/claude-jacked/
GitHub: https://github.com/jackneil/claude-jacked
