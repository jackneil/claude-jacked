---
name: deploy-to-railway
description: >-
  Use when deploying a project to Railway from scratch — provisioning a Postgres
  database, one or more app services (a full-stack monolith OR separate
  frontend/backend), an S3-compatible bucket, sharing credentials + env vars
  between services, wiring GitHub auto-deploy on merge to main, and adding auth
  (Google SSO or Cloudflare Zero Trust in front). Triggers on "deploy this to
  Railway", "get it live on Railway", "set up Railway with a database + auto
  deploy", "split front/back end services on Railway", "add an S3 bucket on
  Railway", "share env vars between Railway services". Carries the hard-won
  gotchas (NIXPACKS Node version, npm-version lockfile skew, RLS-safe DATABASE_URL,
  DB-role password rotation, GitHub App authorization) that turn a 6-hour debug
  into a 20-minute deploy.
---

# Deploy a project to Railway (live, auto-deploying, secure)

This is a **companion to the official `railway:use-railway` skill** — load that
too; it owns the CLI/MCP mechanics (auth, `railway up`, logs, the GraphQL
fallback). THIS skill is the opinionated end-to-end playbook + the gotcha list
that the generic skill doesn't have. When they disagree, the generic skill wins
on a command's exact flags; this skill wins on the *sequence and the traps*.

## The model (read once)

- **Workspace** → billing scope. **Project** → one app + its DB(s). **Service**
  → one deployable (app, DB, or bucket). **Environment** → `production` etc.
- Railway gives every service a **private** address (`<svc>.railway.internal`)
  and, on request, a **public** domain. Services in the same project reach each
  other over the private network — keep databases private.
- **Reference variables** `${{ OtherService.VAR }}` are how you share
  credentials/values between services. Same-service self-refs work too:
  `${{ MY_OWN_VAR }}`.

## Decide the service topology FIRST

| Project shape | Services to create |
|---|---|
| **Full-stack framework** (Next.js, Remix, SvelteKit, Rails, Django+templates) | **ONE** app service. The framework serves UI *and* API from one process. Do NOT split it — two services for one codebase is wrong and leaves a dead/broken half. |
| **Truly separate FE + BE** (React/Vue SPA + a separate Node/Python/Go API) | **TWO** services from the same or two repos. FE gets the public domain; BE stays private (or its own domain); FE reaches BE via `${{ backend.RAILWAY_PRIVATE_DOMAIN }}` or a public URL. Set `NEXT_PUBLIC_API_URL`/`VITE_API_URL` on the FE to the BE's URL. |
| **+ background jobs / cron** | A **separate** service (same repo) with its own config file (cron schedule + start command + `restartPolicy NEVER`). |
| **+ object storage** | A **bucket** (project-level), creds shared to the services that need it. |

If the user says "separate front and back end" but the repo is a full-stack
framework, **tell them it's one service** and why — build the right thing.

## The deploy sequence (CLI; the MCP is often unauthenticated — verify and fall back to CLI)

Prefix Railway CLI calls with `RAILWAY_CALLER=skill:use-railway@<v>` and a stable
`RAILWAY_AGENT_SESSION`. **Background long commands** (`add --database`, `up`) and
**verify with `railway service list`, not the command's stdout** — Railway's CLI
frequently prints `Unauthorized`/errors on a step that actually succeeded.

1. **Auth + workspace**: `railway whoami --json` (lists workspaces). If it fails,
   the user runs `railway login` (their browser).
2. **Project**: `railway init --name <project> --workspace <id> --json` (links cwd).
3. **Postgres**: `railway add --database postgres --json`. ⚠️ It may print
   `Unauthorized` yet still create it — **check `railway service list`**. If you
   ran it twice, you'll have duplicates; delete extras:
   `railway service delete --service <name> --yes`.
4. **App service from GitHub**:
   `railway add --service <name> --repo <owner/repo> --branch main --json`.
5. **Env vars** (see the var recipe below) — set with `--skip-deploys`, all at once:
   `railway variable --service <name> --skip-deploys --set 'K=V' --set 'K2=V2' …`
   Quote `${{…}}` references in single quotes so the shell doesn't expand them.
6. **Domain**: `railway domain --service <name> --json` → returns the public URL.
   Set `APP_ORIGIN` to it (the app needs its own public origin for OAuth
   callbacks, links, WebAuthn RP id).
7. **Deploy + VERIFY**: never report success on `up` returning. Poll
   `railway deployment list --service <name> --limit 1 --json` until `status` is
   `SUCCESS` (deployed) or `FAILED`/`CRASHED` (triage logs). Then `curl` the
   public `/health` (or `/api/health`) endpoint to confirm it's actually serving.
8. **Cron/worker service**: `railway add --service <name> --repo … --branch main`,
   set its env, then set its **Config File Path** to the cron config (e.g.
   `/railway.cron.json`) — that file carries `deploy.cronSchedule` +
   `deploy.startCommand` + `restartPolicy NEVER`. ⚠️ The config-file-path is a
   **dashboard/MCP-only** setting; the CLI can't set it. Tell the user the one
   click, or set it via the `update_service` MCP tool / GraphQL if the MCP is authed.

## The env-var recipe (Postgres + RLS-safe app)

The #1 production security trap: **the app must connect as a non-superuser DB role
with RLS enforced, NOT the Railway default `postgres` superuser** (superusers
BYPASS row-level security → cross-tenant data leak).

- **Migrations** run as the OWNER (create roles, RLS, schema). Reference the
  Postgres service's owner creds:
  `PGHOST=${{ Postgres.PGHOST }}`, `PGPORT`, `PGUSER`, `PGPASSWORD`, `PGDATABASE`
  (all `${{ Postgres.* }}`), plus the target DB name your migrator expects.
- **App runtime** connects as the **app role**, via a CUSTOM `DATABASE_URL` — NOT
  `${{ Postgres.DATABASE_URL }}` (that's the superuser):
  `DATABASE_URL=postgresql://<approle>:<APP_DB_PASSWORD>@${{ Postgres.RAILWAY_PRIVATE_DOMAIN }}:5432/<db>`
- **Rotate the app role's password.** Most apps seed the role with a weak dev
  password. Rotate it to a strong `APP_DB_PASSWORD` (32-byte hex). The cleanest
  spot is the migrator's pre-deploy step: after migrating, run
  `ALTER ROLE <approle> PASSWORD '<APP_DB_PASSWORD>'` (literal-escaped — no bind
  params for passwords; double single-quotes). Then the app's `DATABASE_URL`
  (with `APP_DB_PASSWORD`) connects on the first boot.
- Generate secrets locally, don't hardcode: `node -e "console.log(require('crypto').randomBytes(24).toString('hex'))"`.
- Set `APP_ORIGIN`, any product-name/brand var, and a `CRON_SECRET` if there's a
  cron→app POST.

## Object storage (S3-compatible bucket) + sharing creds

1. `railway bucket create --name <bucket>` (project-level).
2. `railway bucket credentials --bucket <bucket> --json` → endpoint, access key,
   secret, region.
3. Share to the services that need it as variables (or references):
   `S3_ENDPOINT`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_BUCKET`,
   `S3_REGION`. If the bucket exposes them as service vars, prefer
   `${{ <bucket>.* }}` references so a rotation propagates.

## Auth in front of the app

- **Google SSO (app-native)**: the user creates a Google Cloud **OAuth client
  (Web)**; the authorized redirect URI is `${APP_ORIGIN}/api/auth/google/callback`
  (or whatever the app's callback route is — copy the EXACT path from the app, a
  wrong path is the classic launch-day `redirect_uri_mismatch`). They set
  `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET`. You cannot create the OAuth client
  for them — hand off with the exact callback URL filled in.
- **Cloudflare Zero Trust (gateway auth, no app code)**: put a Cloudflare Access
  policy in front of the public domain. The user: add the domain to Cloudflare
  (DNS), create an **Access application** for it, pick an identity provider
  (Google/GitHub/OTP), and write an allow policy (e.g. emails @company). Access
  then challenges every request before it reaches Railway — good when the app has
  no SSO of its own, or for locking a staging/admin URL. (Railway custom domain +
  Cloudflare proxied DNS is the wiring; the Access app is the gate.)

## GOTCHAS (the time-savers — check these the moment a build misbehaves)

1. **Build "schedules" then FAILS with ZERO output → Node version.** NIXPACKS
   can't provision bleeding-edge Node (e.g. 24, 25). If `.nvmrc`/`engines` pins a
   too-new major, the Nix env fails to evaluate before any build log prints. Fix:
   set `NIXPACKS_NODE_VERSION=22` (or another LTS the engines allow). This is the
   single highest-value gotcha — a no-output build failure is almost always this.
2. **`npm ci` "Missing @esbuild/<platform>@x from lock file" → npm version skew.**
   npm 11 (ships with Node 24+) prunes optional per-platform packages from the
   lockfile that npm 10 (ships with Node 22) strictly requires. If you pin Node 22
   on Railway but the lockfile was written by npm 11, `npm ci` fails. Fix:
   regenerate the lockfile with the matching npm —
   `npx -y npm@10 install --package-lock-only` — commit it (it only ADDS platform
   entries, no version changes), verify with `npx npm@10 ci --dry-run`.
3. **`Unauthorized` that actually succeeded.** Railway's CLI/MCP throws auth-y
   errors on `add --database`/provisioning that nonetheless create the resource.
   Always re-verify with `railway service list` before retrying (retrying makes
   duplicates).
4. **MCP unauthenticated even though the CLI is authed.** The Railway MCP server
   has its own token; `railway --help` shows an "Agent tooling" health section. If
   the MCP is `✗`, just use the CLI (don't block on `railway setup agent`, which
   needs a tool restart). Some settings (a service's Config File Path) are only
   reachable via MCP/dashboard, not the CLI.
5. **RLS bypass via DATABASE_URL.** Covered above — the app's `DATABASE_URL` must
   be the app-role URL, never `${{ Postgres.DATABASE_URL }}` (superuser).
6. **GitHub auto-deploy-on-merge needs the Railway GitHub App authorized on the
   repo.** Connecting a repo by name (`add --repo`) sets the metadata, but the
   first build won't auto-fire and merges won't deploy until the user installs/
   authorizes Railway's GitHub App for that repo (railway.com → the service →
   Settings → Source, or Account → Integrations → GitHub). Until then, deploy with
   `railway up` (deploys the local checkout) to get it live, and hand off the
   one-time App authorization to enable the watch.
7. **`railway up` deploys the LOCAL directory**, not GitHub. It's the fastest way
   to a first live build, but it's a manual upload — the GitHub *watch* still needs
   gotcha #6. Use it to validate the pipeline, then enable GitHub auto-deploy.
8. **Verify, don't trust.** A returning `up` only means "build queued." Poll
   `deployment list` to a terminal `SUCCESS`, then `curl` the health endpoint.

## Output

Report: the live URL + health result, every service + its ID, what auto-deploys,
and the precise USER hand-off list (GitHub App auth, any external OAuth/KMS/S3
accounts with exact callback URLs/values, the cron config-file click).
