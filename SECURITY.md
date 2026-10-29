# Security policy

## Report a vulnerability

Do not report a vulnerability in a public issue, a pull request, or a discussion.

1. Open the **Security** tab of this repository.
2. Click **Report a vulnerability** and write the report there. Only the maintainer can read it.
3. If the **Report a vulnerability** button is not on the page, open a public issue with the title "Security contact request". Do not put any vulnerability details in that issue. The maintainer replies with a private way to send the report.

Put these items in the report:

- The jacked version (`jacked check-version` shows it) and your operating system.
- The steps that cause the problem.
- What an attacker can do, and what access the attacker must have first.

## Supported versions

Security fixes go into the newest release on PyPI. Upgrade with `jacked upgrade`, or with `uv tool install claude-jacked --force`. Older releases do not get fixes.

## What jacked protects

jacked stores OAuth credentials for Claude Code and Codex accounts in a local SQLite database under `~/.claude`, and it serves a dashboard on `127.0.0.1:8321`. These are in scope:

- A way for another local user, a web page, or a remote host to read or change those credentials.
- A way to reach the dashboard API from outside the machine when remote access is off.
- A way to make jacked run code that the user did not install.

## How releases are built

- PyPI releases are published from GitHub Actions with Trusted Publishing (no long-lived upload token). Each release build also writes a CycloneDX SBOM, kept as a workflow artifact.
- Every GitHub Action is pinned to a full commit SHA, and the CI tools install from hash-locked requirement files.
- `uv.lock` pins every dependency by hash. A cooldown in `pyproject.toml` (`exclude-newer`) keeps new upstream releases out of the lock for at least seven days.
