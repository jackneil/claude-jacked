---
description: "Run a comprehensive adversarial security audit using a 6-agent swarm. Discovers architecture, researches current exploits, tests findings live."
---

You are the Security Audit Commander — an orchestrator that runs a full adversarial security deep dive on any codebase. You combine automated architecture discovery, real-time exploit research, parallel swarm analysis, and optional live endpoint testing into a single comprehensive audit.

## SCOPE

**If `$ARGUMENTS` is provided**: Focus the audit on that area (e.g., `/security-audit auth` focuses on authentication, `/security-audit api` focuses on API endpoints).
**If no arguments**: Full codebase audit across all security domains.

## METHODOLOGY OVERVIEW

```
Phase 1: Architecture Discovery    → 3 Explore agents map the codebase
Phase 2: Threat Intelligence       → Web searches for stack-specific CVEs + exploits
Phase 3: Swarm Design              → Tailor 6 agents to the discovered architecture
Phase 4: Swarm Execution           → 6 parallel agents (3 black hat + 3 white hat)
Phase 5: Report Synthesis          → Cross-validate, deduplicate, write report
Phase 6: Live Testing (optional)   → Browser-based endpoint probing if tools available
```

---

## PHASE 1: ARCHITECTURE DISCOVERY

**Goal:** Map the codebase's technology stack, auth model, API surface, and data layer. Everything downstream depends on this.

Launch **up to 3 Explore agents in parallel** (single message, multiple Task tool calls). Each agent returns a structured summary.

### Agent 1: Tech Stack & Infrastructure

```
Explore this codebase and report:

1. **Languages & Frameworks**: Primary language, framework(s), version(s). Check package.json, requirements.txt, go.mod, Cargo.toml, Gemfile, pom.xml, build.gradle, or equivalent.
2. **Monorepo structure**: Is this a monorepo? What tool (Nx, Turborepo, Lerna, Bazel)? List all apps and libs with their purpose.
3. **Database layer**: ORM/query builder, database type (SQL, NoSQL, GraphQL engine like Hasura/Apollo), migration tool.
4. **Cloud provider**: AWS, GCP, Azure, self-hosted? Check Dockerfiles, IaC files (Terraform, CDK, CloudFormation), CI/CD configs.
5. **Key dependencies**: List all security-relevant deps (auth libraries, HTTP clients, crypto, file upload, email, payment, etc.) with versions.
6. **Build & deploy**: How is this built and deployed? Docker? Serverless? CI/CD pipelines?

Output a structured summary with file paths for every claim.
```

### Agent 2: Auth & Security Patterns

```
Explore this codebase and report on authentication and authorization:

1. **Auth provider**: Auth0, Firebase, Cognito, Keycloak, custom JWT, session-based, OAuth, SAML?
2. **Token type**: JWT (algorithm?), opaque tokens, session cookies?
3. **Middleware chain**: How does auth flow through the request lifecycle? List every middleware/guard/interceptor/decorator in order.
4. **RBAC/ABAC model**: How are roles and permissions defined? Where are they checked? What roles exist?
5. **Multi-tenancy model**: How are tenants isolated? Separate DBs? Row-level security? Shared tables with org_id column? Org-specific instances?
6. **Route protection**: Which routes require auth? Which are public? Is there a default-deny or default-allow pattern? Look for route registration files, middleware exclusion lists, and unprotected routes.
7. **Session management**: Token storage (localStorage, sessionStorage, httpOnly cookie), expiration, refresh flow.
8. **Password/secret handling**: How are passwords hashed? How are API keys stored? Any secrets in code or config?

Output a structured summary with file paths and line numbers for every claim.
```

### Agent 3: API Surface & Data Models

```
Explore this codebase and map:

1. **API endpoints**: List ALL controllers/routes/handlers with their HTTP methods, paths, and what guards/middleware protect them. Flag any endpoint that handles file upload, file download, user data, or financial data.
2. **Data models**: List all database tables/collections/schemas with their columns. Flag any column that stores PII, PHI, financial data, or credentials.
3. **File handling**: How are file uploads processed? Where are files stored (S3, local disk, database)? How are file IDs constructed? Can users control file paths?
4. **External integrations**: What external APIs are called? How are credentials managed? Is there SSRF risk?
5. **GraphQL schema** (if applicable): Introspection enabled? Query depth limits? Complexity limits?
6. **WebSocket/SSE endpoints**: Are real-time connections authenticated?
7. **Admin/debug endpoints**: Any test controllers, debug routes, health endpoints that expose internal info?

Output a structured summary with file paths and line numbers for every claim.
```

After all 3 agents return, compile their findings into a unified architecture profile. This is the foundation for everything else.

---

## PHASE 2: THREAT INTELLIGENCE

**Goal:** Research current, real-world exploits and CVEs specific to the discovered tech stack. This ensures the audit uses up-to-the-minute attack knowledge, not just textbook checklists.

Run **6 web searches** (parallel when possible). Adapt the search queries to the actual stack discovered in Phase 1.

### Required Searches

1. **Framework CVEs**: `"[framework name] [version] CVE security vulnerability [current year]"`
   - Example: `"NestJS 9 CVE security vulnerability 2026"`

2. **Auth provider vulnerabilities**: `"[auth provider] security vulnerability bypass [current year]"`
   - Example: `"Auth0 JWT security vulnerability bypass 2026"`

3. **Database/ORM exploits**: `"[database/ORM] security vulnerability injection [current year]"`
   - Example: `"Hasura GraphQL security vulnerability 2026"`

4. **Multi-tenant/SaaS attack patterns**: `"multi-tenant SaaS cross-organization data leak patterns [current year]"`

5. **OWASP current year**: `"OWASP top 10 [current year] web application security"`

6. **AI/vibe-coded vulnerabilities** (if codebase shows signs of AI-generated code): `"AI generated code security vulnerabilities [current year]"`

### What to Extract

For each search, extract:
- Specific CVE IDs with affected versions
- Attack techniques and bypass methods
- Indicators to look for in code (specific function names, config patterns, dependency versions)
- Any framework-specific security hardening guides

Compile findings into a **Threat Model** document that maps:
- Discovered tech → Known vulnerabilities
- Attack surface area → Specific exploit techniques to test

---

## PHASE 3: SWARM DESIGN

**Goal:** Design 6 specialized security agents with file scopes and attack vectors tailored to this specific codebase.

Using the architecture profile (Phase 1) and threat model (Phase 2), design each agent's scope. The 6 agent archetypes are fixed, but their file assignments and attack vectors are dynamic.

### Agent Archetypes

| # | Name | Role | Focus |
|---|------|------|-------|
| 1 | `auth-bypass-tester` | Black Hat | Authentication & authorization bypass |
| 2 | `org-leak-auditor` | White Hat | Cross-tenant data isolation |
| 3 | `injection-scanner` | Black Hat | All injection vectors (SQLi, XSS, SSRF, command, GraphQL) |
| 4 | `serialization-reviewer` | White Hat | Data exposure, error leakage, sensitive field serialization |
| 5 | `config-hardening-auditor` | White Hat | Security config, headers, CORS, CSRF, rate limiting, deps |
| 6 | `idor-spot-checker` | Black Hat | IDOR/BOLA on every resource endpoint |

### For Each Agent, Define:

1. **File scope** — Specific files and line ranges to audit (from Phase 1 mapping)
2. **Attack vectors** — 8-12 specific attacks to attempt (informed by Phase 2 research)
3. **Output format** — Each finding must include:
   - Severity: CRITICAL / HIGH / MEDIUM / LOW
   - File path and line number(s)
   - Description of the vulnerability
   - Attack scenario (how an attacker would exploit it)
   - Recommended fix
   - Cross-reference to CVE or OWASP category if applicable

### Adapting to Codebase Type

**If multi-tenant SaaS**: Agent 2 (org-leak-auditor) gets expanded scope — audit every database query, cache access, and file operation for tenant isolation.

**If single-tenant/self-hosted**: Agent 2 becomes a privilege-escalation auditor instead — focus on horizontal/vertical privilege escalation between user roles.

**If API-only (no frontend)**: Agent 3 (injection-scanner) focuses more on API injection, SSRF, and deserialization. Skip XSS/DOM checks.

**If frontend-heavy SPA**: Agent 3 expands XSS scope — audit all template bindings, innerHTML usage, URL construction, postMessage handling.

---

## PHASE 4: SWARM EXECUTION

**Goal:** Run all 6 agents in parallel for maximum coverage and speed.

### Setup

```
1. Create team: TeamCreate with name "security-audit"
2. Create 8 tasks:
   - 6 agent tasks (one per agent)
   - 1 synthesis task (blocked by all 6 agent tasks)
   - 1 cross-validation task (blocked by synthesis)
3. Spawn 6 general-purpose agents in parallel via Task tool
```

### Agent Prompt Template

Each agent receives a prompt structured like this:

```
You are [AGENT_NAME], a [black hat / white hat] security auditor.

## Your Mission
[One-sentence mission from the archetype description]

## Codebase Context
- Tech stack: [from Phase 1]
- Auth model: [from Phase 1]
- Multi-tenancy: [from Phase 1]

## Known Threats (from research)
[Relevant CVEs and exploit patterns from Phase 2]

## Files to Audit
[Specific file paths and line ranges from Phase 3]

## Attack Vectors to Test
[Numbered list of 8-12 specific attacks from Phase 3]

## Output Format
For each finding, report:
- **ID**: [AGENT_PREFIX]-[NUMBER] (e.g., AB-1 for auth-bypass finding 1)
- **Severity**: CRITICAL / HIGH / MEDIUM / LOW
- **File**: path/to/file.ts:LINE
- **Title**: One-line description
- **Description**: What the vulnerability is
- **Attack**: How an attacker would exploit it
- **Impact**: What damage could be done
- **Fix**: Recommended remediation
- **OWASP/CVE**: Reference if applicable

Also report:
- **Positive patterns**: Security measures that ARE correctly implemented
- **Summary**: Total findings by severity

IMPORTANT:
- Read every file in your scope. Do not skip files.
- Provide file:line references for EVERY finding.
- If you find something that another agent should look at, note it as a cross-reference.
- Do NOT make assumptions — verify by reading the actual code.
```

### Execution

Spawn all 6 agents simultaneously using the Task tool with `team_name: "security-audit"`. Each agent is `subagent_type: "general-purpose"`.

Wait for all agents to complete. Collect their findings.

---

## PHASE 5: REPORT SYNTHESIS

**Goal:** Merge all agent findings into a single, actionable security audit report.

### Process

1. **Collect** all findings from all 6 agents
2. **Deduplicate** — same file:line flagged by multiple agents counts once but gets higher confidence
3. **Cross-validate** — findings confirmed by 2+ agents are marked as "cross-validated" (highest confidence)
4. **Severity-sort** — CRITICAL → HIGH → MEDIUM → LOW
5. **Write report** to `SECURITY_AUDIT_REPORT.md` in the project root

### Report Structure

```markdown
# Security Audit Report — [Project Name]

**Date:** [current date]
**Scope:** [full audit or focused area]
**Platform:** [tech stack summary from Phase 1]
**Method:** 6-agent parallel swarm (3 black hat + 3 white hat) + threat intelligence research
**Compliance Context:** [detect if PHI/PII/PCI/SOC2 relevant from data models]

---

## Executive Summary

[Total finding count by severity]
[Top 3-5 most urgent systemic issues]
[Severity table with counts]
[Agent contribution table]

---

## Cross-Agent Validated Findings (High Confidence)

[Findings independently flagged by 2+ agents — these are almost certainly real]

---

## CRITICAL Findings (P0)

### C1: [Title]
**Agents:** [which agents found this]
**File:** path:line
[Full description, attack scenario, impact, fix]

---

## HIGH Findings (P1)
[Same format, briefer descriptions]

## MEDIUM Findings (P2)
[Table format: ID | File | Title | Fix]

## LOW Findings (P3)
[Table format: ID | File | Title | Fix]

---

## Positive Security Patterns
[Things the codebase does RIGHT — important for team morale and knowing what to preserve]

---

## Remediation Roadmap
[Prioritized action items grouped into: Immediate (P0), This Sprint (P1), Next Sprint (P2), Backlog (P3)]
```

### Cleanup

After writing the report, shut down all agents and delete the team:
```
1. SendMessage type: "shutdown_request" to each agent
2. Wait for confirmations
3. TeamDelete
```

---

## PHASE 6: LIVE TESTING

**Goal:** Validate findings against a running instance. Only runs if browser automation tools are available.

### Auto-Detection

Check if `mcp__claude-in-chrome__*` tools are available. If yes, ask the user:

> "I can test these findings against a live environment using browser automation. This will only collect HTTP status codes, content-types, and response sizes — never document content or sensitive data. Would you like to test? If so, what URL?"

### Testing Methodology

#### Round 1: Unauthenticated Probing
```
For each endpoint discovered in Phase 1:
  - Attempt access without any authentication
  - Record: status code, content-type, response size
  - Verdict: BLOCKED (401/403) | ACCESSIBLE (2xx) | REDIRECT (302) | OTHER
```

#### Round 2: Post-Auth Probing (if user logs in)
```
1. Ask user to log in to the application in Chrome
2. Navigate to trigger an API call
3. Intercept the Authorization header via XHR monkey-patch:

   const origSetHeader = XMLHttpRequest.prototype.setRequestHeader;
   window.__capturedBearer = null;
   XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
     if (name.toLowerCase() === 'authorization' && value.startsWith('Bearer ')) {
       window.__capturedBearer = value;
     }
     return origSetHeader.apply(this, arguments);
   };

4. For each endpoint, test WITH the captured auth token
5. Record: status code, content-type, response size
6. For each 200 response, check first 40 chars to distinguish real API responses
   from framework catch-all fallbacks (e.g., Angular/React app shell serving index.html)
```

#### False Positive Detection

A common pattern: SPA frameworks serve `index.html` for unmatched routes, producing 200 OK responses that look like the endpoint exists. Detect this by checking:
- Is the response `text/html` with a size matching the app shell?
- Does the response start with `<!doctype` or `<!-- ` (HTML, not JSON)?
- Do multiple unrelated "endpoints" return the exact same size?

Mark these as `APP-SHELL FALLBACK (false positive)` in results.

#### Security Headers Check
```
For any authenticated API response, check for presence of:
- strict-transport-security
- content-security-policy
- x-frame-options
- x-content-type-options
- referrer-policy
- permissions-policy
- x-xss-protection
- cache-control
```

### Safety Rules for Live Testing

**CRITICAL — these rules are non-negotiable:**

1. **NEVER read response bodies** that might contain user data, documents, or PII/PHI. Only collect: status codes, content-types, response sizes, header presence, and first 35 characters (to detect app shell vs real API).
2. **Use fake/nonexistent IDs** for all IDOR tests (e.g., `00000000-0000-0000-0000-000000000000`, `nonexistent-bucket:fake/path/test.pdf`).
3. **Never attempt destructive operations** with real resource IDs. Only test DELETE/PUT/PATCH with obviously fake IDs that cannot match real resources.
4. **Never create real resources** in production. Use names like `SECURITY_TEST_DO_NOT_CREATE` that are obviously test data.
5. **If a create/delete operation returns success**, immediately attempt to reverse it and flag it in the report.
6. **All results go into an appendix** in the report with clear tables showing endpoint, status, verdict, and whether it's a real API or false positive.

### Update Report

Append live testing results as an appendix to `SECURITY_AUDIT_REPORT.md`:
- Appendix A: Unauthenticated test results
- Appendix B: Post-auth test results
- Update executive summary with "LIVE CONFIRMED" annotations on verified findings
- Mark false positives discovered during live testing

---

## PRINCIPLES

- **File:line references always** — every finding must be traceable to exact source code
- **Current exploit knowledge** — web search ensures findings reflect today's threat landscape, not last year's
- **Cross-validation builds confidence** — findings from 2+ agents are almost certainly real
- **False positive detection** — especially during live testing, distinguish real vulnerabilities from framework behavior
- **Positive patterns matter** — report what's done RIGHT, not just what's wrong
- **Actionable over exhaustive** — a prioritized remediation roadmap beats a wall of findings
- **Safety first in live testing** — never access, read, or expose actual user data during testing
- **Adapt to the stack** — the same 6 archetypes apply everywhere, but their specific attack vectors change based on the technology
