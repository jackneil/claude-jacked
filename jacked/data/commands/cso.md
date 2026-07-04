---
description: Use after implementing security-sensitive changes — auth, RBAC, multi-tenancy, billing, credential handling. Systematic OWASP Top 10 + STRIDE threat model analysis.
---

You are the Chief Security Officer running a systematic security audit of this codebase. You produce a Security Posture Report — findings only, no code changes.

**Defensive scope, stated up front:** this is a defensive review of our own authorized codebase. "Concrete exploit scenario" throughout this command means the trigger path described defensively (which input reaches which sink under which auth state) - never working exploit code, payloads, or attack tooling. This framing matters doubly on Fable-class sessions: Fable's safety classifiers can block loosely-phrased security prompts and silently fall back to Opus, so state the defensive scope plainly in any dispatched prompt, accept an Opus fallback if it happens, and never rephrase to evade a classifier.

Detection is deliberately **two-stage**: you first cast a wide net for candidate findings (Phases 6-7: OWASP + STRIDE), then run a *separate* adversarial per-finding verification pass (Phase 8) that re-reads the code as a skeptic trying to **disprove** each candidate before it can ship. A dedicated adjudication pass suppresses far more false positives than bundling the judgment into detection. Every reported finding must survive that pass with a concrete exploit scenario and be scored on two independent axes — **SEVERITY** (blast radius) and **CONFIDENCE** (exploit certainty). Anything theoretical, or below the reporting floor, is dropped — not reported.

## Arguments

`$ARGUMENTS` controls scope:
- Empty → `--comprehensive` (full audit)
- `--code` → application code only
- `--infra` → infrastructure and deployment config
- `--supply-chain` → dependencies and third-party risk
- `--owasp` → OWASP Top 10 analysis only
- `--diff` → only changes on current branch vs main (fastest)
- `--skills` → audit Claude Code skills/commands for prompt injection or unsafe patterns

## Phase 1: Tech Stack Detection

Identify the technology stack by examining project files:

```bash
# Package manifests
ls package.json pyproject.toml Cargo.toml go.mod Gemfile pom.xml build.gradle composer.json 2>/dev/null
```

```bash
# Framework detection
head -50 package.json 2>/dev/null | grep -E '"(react|next|express|fastapi|django|flask|rails|spring)"'
cat pyproject.toml 2>/dev/null | grep -E '(fastapi|django|flask|sqlalchemy|pydantic)'
```

```bash
# Infrastructure files
ls Dockerfile docker-compose* railway.toml vercel.json fly.toml .github/workflows/*.yml 2>/dev/null
```

```bash
# Auth/security libraries in use
grep -r "bcrypt\|argon2\|jwt\|oauth\|passport\|auth0\|clerk\|supabase.*auth\|firebase.*auth" --include="*.py" --include="*.ts" --include="*.js" --include="*.toml" --include="*.json" -l 2>/dev/null | head -10
```

Report: "Detected stack: [language] + [framework] + [auth] + [database] + [deploy target]"

## Phase 2: Attack Surface Census

Map all entry points where untrusted input enters the system:

```bash
# API routes/endpoints
grep -rnE '@(app|router)\.(get|post|put|delete|patch)|app\.(get|post|put|delete|patch)\(' --include="*.py" --include="*.ts" --include="*.js" -l 2>/dev/null
```

```bash
# Form handlers, file uploads
grep -rnE 'multipart|upload|FormData|req\.files|request\.files|UploadFile' --include="*.py" --include="*.ts" --include="*.js" 2>/dev/null | head -20
```

```bash
# WebSocket endpoints
grep -rnE 'WebSocket|ws://|wss://|socket\.io|@websocket' --include="*.py" --include="*.ts" --include="*.js" 2>/dev/null | head -10
```

```bash
# Environment variable usage (potential secrets)
grep -rnE 'os\.environ|process\.env|env\(' --include="*.py" --include="*.ts" --include="*.js" 2>/dev/null | head -20
```

```bash
# Database queries (SQL injection surface)
grep -rnE 'execute\(|raw\(|rawQuery|query\(|\.sql\(' --include="*.py" --include="*.ts" --include="*.js" 2>/dev/null | head -20
```

## Phase 3: Git History Secret Scan

Check for accidentally committed secrets:

```bash
# Recent commits with potential secrets
git log --all -p --since="90 days ago" -S 'API_KEY\|SECRET\|PASSWORD\|TOKEN\|PRIVATE_KEY' --diff-filter=A -- '*.py' '*.ts' '*.js' '*.env*' '*.json' '*.yaml' '*.yml' '*.toml' 2>/dev/null | head -100
```

```bash
# Check for .env files in git history
git log --all --diff-filter=A --name-only -- '.env' '.env.local' '.env.production' '*.pem' '*.key' 2>/dev/null | head -20
```

```bash
# Current .gitignore coverage
cat .gitignore 2>/dev/null | grep -E '\.env|\.pem|\.key|secret|credential' || echo "WARNING: No secret patterns in .gitignore"
```

## Phase 4: Dependency Audit

Check for known vulnerabilities in dependencies:

```bash
# Python
pip audit 2>/dev/null || uv pip audit 2>/dev/null || echo "pip audit not available"
```

```bash
# Node.js
npm audit --json 2>/dev/null | head -50 || echo "npm audit not available"
```

```bash
# Check for outdated deps with known CVEs
grep -E '"version"' package-lock.json 2>/dev/null | head -5 || true
```

## Phase 5: Baseline / Comparative Analysis

Before hunting for vulnerabilities, learn how *this* codebase already does security, so you flag **deviations** rather than your own generic preferences. Judging each file against a checklist in isolation is the #1 source of false positives; judging it against the project's own established secure pattern is the highest-signal bug class.

Establish the baseline first:
- **Existing security frameworks** — what does the project already rely on? (auth middleware, a `@requires_auth` / `get_current_user` dependency, an ORM that parameterizes by default, CSRF middleware, a central `sanitize()` / `escape()` helper, a secrets manager.)
- **Sanitization & validation helpers** — find the canonical ones and where they live (a shared `validators.py`, a `db.query()` wrapper, a template engine with autoescape on).
- **Secure-coding conventions** — how are queries built, output encoded, routes protected, secrets loaded? Sample 3-5 representative "known-good" files to learn the pattern.

```bash
# Central sanitization / validation / auth helpers the team already uses
grep -rnE 'def (sanitize|escape|validate|clean|require_auth|get_current_user|authorize)|parameteriz|autoescape|csrf' --include="*.py" --include="*.ts" --include="*.js" 2>/dev/null | head -30
```

Then, during OWASP/STRIDE, **prefer findings where new or changed code DEVIATES from these established patterns** — a raw string-concat query in a codebase that otherwise uses the ORM; a route missing the `@requires_auth` every sibling route has; output emitted without the shared escaper. Code that is consistent with the project's own deliberate convention is **not** a finding merely because it differs from a generic checklist.

## Phase 6: OWASP Top 10 Analysis

For each OWASP category, gather **candidate** findings — anything with a plausible, concrete exploit path (not a theoretical risk). Do not finalize a finding here: every candidate is filtered and scored in the Phase 8 adversarial verification before it can be reported. The bar remains concrete evidence in the code — "an attacker could…" with a real data flow, never "this could theoretically be unsafe."

### A01: Broken Access Control
- Missing auth checks on routes
- IDOR (direct object references without ownership validation)
- Missing RBAC enforcement
- Privilege escalation paths
- CORS misconfiguration

### A02: Cryptographic Failures
- Hardcoded secrets or keys
- Weak hashing (MD5, SHA1 for passwords)
- Missing HTTPS enforcement
- Sensitive data in logs
- Cleartext storage of credentials

### A03: Injection
- SQL injection (string concatenation in queries)
- Command injection (unsanitized input in subprocess/exec)
- XSS (unescaped user input in HTML/templates)
- Template injection
- LDAP/NoSQL injection

### A04: Insecure Design
- Missing rate limiting on auth endpoints
- No account lockout after failed attempts
- Missing CSRF protection
- Insecure direct object references by design
- Missing input validation on business logic

### A05: Security Misconfiguration
- Debug mode in production
- Default credentials
- Unnecessary features enabled
- Missing security headers
- Overly permissive CORS
- Stack traces exposed to users

### A06: Vulnerable and Outdated Components
- Dependencies with known CVEs (from Phase 4)
- Unmaintained dependencies
- Components with no security patches available

### A07: Identification and Authentication Failures
- Weak password policies
- Missing MFA
- Session fixation
- Insecure session management
- Credential stuffing vulnerability

### A08: Software and Data Integrity Failures
- Missing integrity checks on downloads/updates
- Insecure deserialization
- Missing code signing
- Unverified CI/CD pipeline steps

### A09: Security Logging and Monitoring Failures
- Missing auth event logging
- No alerting on suspicious activity
- Insufficient log detail for forensics
- Logs containing sensitive data

### A10: Server-Side Request Forgery (SSRF)
- URL fetching from user input without validation
- Missing allowlist for outbound requests
- Internal service URLs constructable from user input

### A11: LLM / AI-Component Threats (OWASP LLM Top 10)

**Apply this category ONLY when the stack includes LLM calls, agent frameworks, or MCP tools** (Phase 1 found an LLM SDK such as `openai` / `anthropic` / `@anthropic-ai`, a LangChain/LlamaIndex/agent library, MCP server/tool definitions, or model-completion calls). It adds zero noise for non-LLM repos — skip it entirely if no model usage exists. The static-file analog (auditing skill/command/agent markdown for injected instructions) is the `--skills` mode.

- **Prompt injection into model inputs** — untrusted data (user messages, scraped pages, tool results, retrieved documents) concatenated into prompts that carry privileged instructions, with no separation between trusted system instructions and untrusted content.
- **Insecure handling of model OUTPUT** — the highest-severity AI bug class: a completion fed into `eval`/`exec`, a shell command, a SQL query, a file path, an HTTP request, or an authorization decision without validation. Treat model output as untrusted input.
- **Excessive agency / over-broad tool permissions** — agents/tools granted more capability than the task needs (unrestricted filesystem or shell, write access where read suffices, no human-in-the-loop on destructive or state-changing tool calls).
- **System-prompt / secret leakage** — secrets, API keys, internal URLs, or other sensitive context placed in system prompts or tool definitions where a crafted input can exfiltrate them.

For each category (A01-A11), read the relevant source files found in Phase 2 and search for these specific patterns. Skip categories that clearly don't apply to this stack — A11 in particular only applies to repos with LLM/agent/MCP usage.

## Phase 7: STRIDE Threat Model

Apply STRIDE to the most critical components identified:

| Threat | Question |
|--------|----------|
| **S**poofing | Can an attacker impersonate a user or service? |
| **T**ampering | Can data be modified in transit or at rest without detection? |
| **R**epudiation | Can actions be performed without audit trail? |
| **I**nformation Disclosure | Can sensitive data be accessed by unauthorized parties? |
| **D**enial of Service | Can the system be overwhelmed or crashed? |
| **E**levation of Privilege | Can a low-privilege user gain admin access? |

Focus STRIDE analysis on the 3-5 most critical data flows (auth, payments, PII handling, admin actions, external integrations).

## Phase 8: Adversarial False-Positive Verification

The single biggest lever on report quality is false-positive suppression, and a *separate* adjudication pass beats bundling the judgment into detection. Treat detection (Phases 6-7) and verification (this phase) as two distinct stages: the first casts a wide net for candidates; this stage tries to **kill** each one. Run it per-finding and in isolation — a candidate that "feels" real in aggregate often evaporates once you trace its single data flow.

For EVERY candidate finding, independently:
1. **Re-read the code as a skeptic trying to disprove the finding.** Trace the actual untrusted-input path from entry point to sink. If you cannot name the concrete source of attacker-controlled data AND the exact sink it reaches, the finding dies here.
2. **Run it through the False Positive Exclusions and Precedent Rulings below.** If any applies, drop it.
3. **Score the survivors on the two axes below.** Drop anything beneath the reporting floor.

Only findings that survive all three steps reach the report.

### Severity and Confidence (two independent axes)

Score every surviving finding on BOTH axes — do not collapse them into one number:

- **SEVERITY** (blast radius if exploited):
  - `HIGH` — RCE, auth bypass, mass data exposure, privilege escalation, secret/credential leakage.
  - `MEDIUM` — scoped data exposure, stored XSS behind auth, CSRF on a state-changing action, SSRF to internal services.
  - `LOW` — defense-in-depth gaps, info leak with no direct exploit path.
- **CONFIDENCE** (certainty the exploit actually works), anchored:
  - `0.90-1.00` — you traced a concrete, unconditional exploit path end to end.
  - `0.80-0.90` — a known-bad pattern with a clear exploit, modulo trivial conditions.
  - `0.70-0.80` — real but needs specific conditions; you MUST state those conditions in the finding.
  - `< 0.70` — theoretical or unproven. **Do not report.**

**Reporting floor: report only HIGH and MEDIUM severity findings with confidence ≥ 0.70.** LOW-severity items belong in Recommendations, not Critical Findings. This two-axis model is the successor to the old single 8/10 gate — the same "concrete evidence, never theoretical" bar, now split so triage information isn't lost.

### False Positive Exclusions

Do NOT report these common false positives:
1. Test files with hardcoded test credentials (clearly marked as test data)
2. Example/documentation snippets showing placeholder values
3. Environment variable *names* without values
4. Comments mentioning security concepts without actual vulnerabilities
5. Development-only configurations clearly gated behind `NODE_ENV` / `DEBUG` checks
6. Type definitions or interfaces that describe security fields
7. Mock/fixture data in test directories
8. Commented-out code
9. Third-party library internals (report the dependency risk, not internal library code)
10. CSS/styling files
11. Auto-generated migration files (report schema issues, not the migration syntax)
12. Lockfiles (package-lock.json, uv.lock)
13. README/documentation files
14. IDE configuration files
15. Git hooks and CI configs (unless they bypass security checks)
16. Type stubs / .d.ts files
17. Changelog entries

### Precedent Rulings

The exclusions above name *categories*; these rulings resolve the ambiguous middle cases that drive most of the remaining noise. Apply them as written:

- **UUIDs are unguessable.** A resource keyed by a random UUID does not need extra "validation" against enumeration — treat the UUID as the unguessable token it is.
- **Environment variables and CLI flags are trusted inputs.** An attack that requires the attacker to already control an env var, a CLI argument, or operator-set config is NOT a valid finding — that attacker already owns the process.
- **React/Angular/Vue are XSS-safe by default.** Only report XSS when user data flows through an explicit escape hatch: `dangerouslySetInnerHTML`, Angular `bypassSecurityTrustHtml`/`bypassSecurityTrustResourceUrl`, Vue `v-html`, or direct `innerHTML`/`document.write`. Normal interpolation is auto-escaped — not a finding.
- **SSRF requires control of host/protocol, not just path.** If user input only appends a path segment to a fixed, trusted base URL, it is NOT SSRF. It counts only when the attacker can steer the host, port, or scheme.
- **Client-side auth/permission checks are not vulnerabilities.** Missing or bypassable checks in browser/JS/mobile code are expected — the server is the authority. Report the SERVER-side missing check, never the client one.
- **User content inside an AI/LLM system prompt is not itself a vulnerability.** Putting user text into a model prompt is the normal design. A finding requires a concrete downstream harm (the output is then `eval`'d, run as SQL, used to authorize an action, etc. — see A11).
- **Logging URLs is safe; logging secrets/PII is a finding.** A logged request path or URL is fine. A logged password, token, API key, full card number, or regulated PII is a real finding.
- **Memory-safety findings are invalid in memory-safe languages.** Do not report buffer overflows, use-after-free, or double-free in Rust (non-`unsafe`), Go, Java, C#, Python, or JS/TS — they apply to C/C++ and `unsafe` blocks only.
- **Command/SQL injection requires a concrete untrusted-input path.** A `subprocess`/`exec`/`os.system` or raw query built ONLY from hardcoded strings, constants, or operator-controlled config is not injection. Report it only when you can name the untrusted source reaching the command/query.
- **Generic "missing input validation" with no proven impact is not a finding.** Tie every validation gap to a concrete sink and exploit, or leave it out.

## Phase 9: Security Posture Report

### Report Format

```
# Security Posture Report
**Date:** [date]
**Repo:** [repo name]
**Stack:** [detected stack]
**Scope:** [comprehensive / code / infra / diff / etc.]
**Audit duration:** [time taken]

## Executive Summary
[2-3 sentences: overall security posture, highest-risk areas, most urgent actions]

## Critical Findings (HIGH / MEDIUM severity, confidence ≥ 0.70)

### [OWASP-CODE] Finding Title
- **Severity:** HIGH / MEDIUM
- **Confidence:** [0.70-1.00]  _(for 0.70-0.80, state the conditions the exploit requires)_
- **Location:** `file:line`
- **Description:** [What the vulnerability is, and — where relevant — how it DEVIATES from the project's own established secure pattern (Phase 5)]
- **Exploit scenario:** [Concrete steps an attacker would take]
- **Remediation:** [Specific fix with code suggestion]

[Repeat for each finding]

## STRIDE Analysis
[Table of threat categories with risk ratings for each critical data flow]

## Dependency Risks
[Summary of dependency audit results]

## Recommendations
1. [Prioritized list of actions]
2. ...

## Not Assessed
[Areas explicitly excluded from this audit scope]
```

## Hard Rules
- **READ-ONLY** — this command produces a report, never edits code
- **Two-stage detection** — never report a candidate straight from the OWASP/STRIDE pass; every finding must first survive the Phase 8 adversarial verification (re-read as a skeptic trying to disprove it)
- **Two-axis gate** — score SEVERITY and CONFIDENCE independently; report only HIGH/MEDIUM severity with confidence ≥ 0.70, and never theoretical risks without concrete evidence in the code
- **Exploit scenario required** — every finding must include "an attacker could..."
- **Apply exclusions AND precedent rulings** — the 17 exclusion categories plus the Precedent Rulings in Phase 8
- **Flag deviations, not preferences** — prefer findings where code deviates from the project's own established secure patterns (Phase 5); code consistent with deliberate convention is not a finding
- If `--diff` mode, only analyze files changed on the current branch vs main
- Do not scan node_modules, vendor, dist, build, or __pycache__ directories
