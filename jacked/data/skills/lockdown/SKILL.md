---
name: lockdown
description: Use when auditing or hardening a repository against software supply-chain attacks — including dependency lockfile integrity, CVE scanning, malware/typosquat detection, GitHub Actions SHA pinning, secrets scanning, provenance/signing, and SLSA/HIPAA control mapping. Triggers on phrases like "lock down dependencies", "supply chain audit", "is this repo secure", "dependency poisoning", "typosquat", "harden actions", "pin actions", "audit deps", or any concern about consuming or shipping third-party code safely.
---

First, check if a repo-scoped version exists in the current project:
1. If `.claude/skills/lockdown/SKILL.md` exists (Glob) → read and follow it instead of this file.
2. If `.claude/commands/lockdown.md` exists (Glob) → read and follow it instead.
3. Otherwise, read `~/.claude/commands/lockdown.md` and follow it.
