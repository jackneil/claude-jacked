# OAuth and Credential Lifecycle: Canonical Architecture

**Last source-wide audit:** 2026-09-03

**Status:** Living architecture contract for the account-truth implementation.

## 1. Truth is split across explicit axes

Jacked does not use one overloaded "active account" value. The transaction
model and persistence distinguish these facts:

| Axis | Meaning | Source |
| --- | --- | --- |
| Desired default | The account requested by a credential activation | `desired_account_id` and `SwitchResult.desired_default` |
| Storage observation | The identity read back from the certified credential-store topology | `SwitchResult.storage` and `SwitchResult.observed_identity` |
| Committed authority | A target finalized under a cooperative capability and complete writer fence | `SwitchResult.committed_authority` |
| Existing-session activation | What may be claimed about already-running Claude processes | `SwitchResult.existing_session_activation` |
| Provider verification | Whether a provider request proved the identity actually used | `SwitchResult.provider_verification` |

The implementation currently leaves provider verification as `unverified`. A
successful credential readback is not proof that an already-running request used
that credential. The HTTP switch response exposes storage, committed authority,
existing-session activation, and provider verification. It does not expose the
result model's desired-default axis.

The old file-first fallback stack is no longer canonical. Code that needs the
current Claude identity calls `resolve_active_identity()` or a supplied
`CanonicalCredentialResolver`. Unsupported, missing, unusable, or conflicting
observations do not fall back to a database preference or a guessed file stamp.

## 2. OAuth token model

Each Claude account row keeps two token sets:

| Fields | Consumer | Publication rule |
| --- | --- | --- |
| `access_token`, `refresh_token`, `expires_at` | Jacked profile, usage, and API-key flows | Database state; the primary refresh token is not handed to Claude Code as a fallback refresh token |
| `cc_access_token`, `cc_refresh_token`, `cc_expires_at` | Claude Code | Used to build the credential payload when present |

`build_oauth_data()` prefers the Claude Code token set. If it must fall back to
the primary access token, it publishes a null `refreshToken` so Claude Code
cannot rotate Jacked's primary refresh token.

OAuth completion saves the account in SQLite before any local activation. It
attempts local credential activation only when the new or re-authorized account
should be the default and the request came from a loopback client. For a remote
dashboard, `_complete_auth()` saves the account, skips host credential mutation,
and puts `activation_status=local_only` in its internal completion result.

`OAuthFlow.get_status()` copies `activation_status`,
`activation_operation_id`, and `activation_message` from the completed result
when those fields exist. Browser and manual-flow polling clients therefore see
the same evidence-qualified activation outcome. A remote save without host
mutation reports `local_only` and has no activation operation ID.

Adding another account does not steal the current default. `_should_become_active()`
returns true only for the first valid account, a replacement for a missing
default, or re-authorization of the current default.

## 3. Canonical credential payload

`CredentialPayload` in `jacked/credentials/canonical.py` is the common
representation used by stores, the resolver, and the transaction engine. It:

- accepts only a JSON object;
- rejects duplicate object keys and non-finite numbers;
- canonicalizes with sorted keys and compact UTF-8 JSON;
- derives a SHA-256 digest from those canonical bytes;
- derives identity only from a positive integer `_jackedAccountId` and the
  optional `claudeAiOauth.organizationUuid`;
- keeps credential contents out of its `repr`.

The canonical digest compares complete payloads. It is not written into the
secret-free resolver snapshot.

## 4. Capability resolution fails closed

Before the new activation path can mutate credentials, it resolves the actual
Claude executable, runs `claude --version`, follows the executable to its real
path, and hashes its bytes. `CapabilityRegistry` then looks up a certified
credential-store topology for that identity on:

- operating system;
- config mode (`global` or `scoped`).

The reported build must also be at or above the record's certified floor. A
build past the record's "inspected through" version still matches and adds the
`build-newer-than-inspected` marker to the resolution evidence.

The installed path, the executable SHA-256, the machine architecture, and the
reported build are carried on the resolved identity and recorded as evidence;
none of them is part of the registry key. A new Claude release or a relocated
binary therefore does not by itself invalidate a match.

An unknown platform or config mode, or a build below the certified floor,
resolves to `unsupported` with `can_mutate=False`; digest and architecture are
evidence, not gates. A registry kill switch also disables mutation until a
caller explicitly begins a fresh resolution generation. There is no "closest
known version" or platform fallback.

### Shipped capability records

Certification is keyed by credential-store topology per platform and config
mode, not by executable bytes. The observed build and SHA-256 are recorded as
evidence on every resolution.

| Platform | Config mode | Build floor | Inspected through | Authority | Required mirror | Mode |
| --- | --- | --- | --- | --- | --- | --- |
| `darwin` | `global` | `2.1.0` | `2.1.260` | macOS Keychain (`Claude Code-credentials`) | `~/.claude/.credentials.json` | `global_uncooperative` |
| `linux` | `global` | `2.1.0` | `2.1.260` | `~/.claude/.credentials.json` | none | `global_uncooperative` |
| `windows` | `global` | `2.1.0` | `2.1.260` | `%USERPROFILE%\.claude\.credentials.json` | none | `global_uncooperative` |

A build newer than "inspected through" still resolves and carries the
`build-newer-than-inspected` evidence marker; on such a build jacked refuses
to create a missing authority, because a moved store looks identical to a
missing one. Every unfenced activation, on any build, logs the `claudeAiOauth`
keys the authority carries that jacked would drop. Scoped config mode
(`CLAUDE_CONFIG_DIR`) has no shipped record. On Linux and Windows a scoped
launch does not touch the global file. `~/.claude` must be a real directory
(a symlinked dotfiles setup is refused with a clear reason); on Windows the
file's privacy is the profile directory's ACL, the same as Claude Code's own.

## 5. Store topology

### 5.1 SQLite is account inventory and journal storage

`~/.claude/jacked.db` stores account token sets, preferences, action
idempotency records, credential transaction records, session observations,
decision logs, and swap logs. A desired database pointer is not proof of the
credential authority's current contents.

`DatabaseCredentialSwitchRepository` deliberately serializes no credential
payload. Cooperative pending rows contain transcript-bound HMACs for the
before and target canonical digests, plus capability and machine metadata.

### 5.2 macOS Keychain is authority for the shipped capability

For a certified macOS build, `MacOSCredentialStore` accesses the generic
password item whose service is `Claude Code-credentials` and whose account is
the username returned by the operating-system identity API, not the `USER` or
`USERNAME` environment variables.

#### Keychain access

All Keychain reads and writes go through `/usr/bin/security`, the same
Apple-signed tool Claude Code uses, so its access-list entry is shared and no
password prompt ever names a Python binary. Writes run `security -i` with the
command on stdin, so tokens are not process arguments on the default path. A
write is sent as hex when that command line fits the tool's 4095-byte stdin
limit, then as escaped JSON with `-w` when that form fits; otherwise the write
fails closed unless `JACKED_KEYCHAIN_ARGV_FALLBACK=1` is set, which passes the
hex payload as a process argument and warns once per process. Reads decode the
tool's hex output. Background calls are guarded by a prompt-free lock-status probe, a 2 s
subprocess timeout (the child is killed on expiry), and a 10 minute cooling
latch that also blocks background writes; a successful foreground call clears
the latch.

Creating a missing Keychain item requires `InteractionMode.FOREGROUND`; a
background request gets `interactive_required`. Every successful update or add
is read back and must match the requested canonical digest.

### 5.3 Global credential file is a required mirror

For the shipped capability, `~/.claude/.credentials.json` must agree byte-for-
byte after canonicalization with the Keychain authority. `FileCredentialStore`
uses private staging, flush and fsync, durable replacement, owner-only modes,
and link/reparse-point checks. It rejects paths outside the trusted root,
symlinks, hard links, and non-regular targets.

### 5.4 Metadata and per-account directories are not authorities

`~/.claude.json` contains identity and tier metadata used by Claude Code's UI.
It is not consulted by the canonical active-credential resolver.

`~/.claude/accounts/<id>/` is still prepared for `jacked claude <id>`, including
a per-account `.credentials.json` and selected shared resources. The directory
is launch input, not a certified authority for the currently shipped build.

### 5.5 Claude config identity mirror

Claude Code holds its OAuth token in memory. It reads the credential store
again only when the `oauthAccount` identity in `~/.claude.json` changes. A
credential write that does not change that identity is invisible to a running
session. The session keeps the previous account until it stops.

Every switch therefore republishes the identity. The transaction engine
publishes it after a committed switch and after an observed unfenced switch.
Crash recovery publishes it when it finds the target credentials in the
authority. The engine replaces all identity fields together. It removes a
`displayName` that the new account does not supply, because the previous
account's name must not stay beside the new email address.

A failed publication does not undo the switch. The credentials are already in
the store. The result degrades instead:

| Fact | Value after a failed publication |
| --- | --- |
| Outcome | `committed_degraded`, or `observed_target_unfenced` |
| Existing-session activation | `restart_required` |
| Message | contains "claude config identity not updated" |

The switch lease is cross-process. `jacked launch` activates an account from
one process while the dashboard service can activate one from another process.
A lock file at `~/.claude/jacked-service-v2/credential-switch.lock` separates
the processes. A thread lock separates the threads inside one process. A switch
holds both leases. Neither lease waits: a switch that cannot get a lease
reports `interactive_operation_in_progress` and changes nothing.

## 6. Canonical resolution

`CanonicalCredentialResolver.resolve()` reads the declared authority and every
required mirror. Resolution is intentionally strict:

| State | Condition |
| --- | --- |
| `resolved` | Every declared store returns a payload, all canonical digests and identities agree, and the account stamp is present |
| `missing` | A declared store reports no credential |
| `unusable` | An adapter is absent, a store cannot be read, a payload is invalid, or the agreed payload lacks an account stamp |
| `conflict` | Store digests or identities disagree |
| `stale` | Reserved observation state; the current fresh snapshot reader returns no snapshot after expiry rather than constructing this state |
| `unsupported` | No certified topology for this platform and config mode, or the build is below the certified floor |

`GET /api/auth/active-credential` returns this state and its evidence. Even a
resolved stamp is checked against a live, non-deleted Claude account row and
against the stored organization UUID before the endpoint returns an account ID
and email.

`read_active_account_id()` follows the same rule. It returns an integer only
for `resolved`; it returns `None` for every other state. Callers cannot silently
restore file precedence.

## 7. Credential transaction outcomes

`CredentialTransactionEngine.activate()` validates that the payload's account
stamp matches the request before any write.

### Cooperative capabilities

A cooperative switch requires a complete writer inspection and matching
protocol and capability epochs for every active writer. Under the process-wide
switch lease, it then:

1. reads the authority's before state;
2. obtains the private installation recovery key;
3. creates a secret-free pending journal record before mutation;
4. writes and reads back the authority;
5. rechecks the writer fence;
6. publishes required and optional mirrors;
7. atomically finalizes the journal, `active_account_id`,
   `desired_account_id`, and audit rows;
8. publishes a short-lived, secret-free resolver snapshot.

A required-mirror failure is `indeterminate`. An optional metadata failure can
produce `committed_degraded`. Recovery observes the authority and classifies a
pending operation as target committed, previous value preserved, or
indeterminate. Recovery never rewrites credentials.

### Shipped global-uncooperative capability

The certified Claude build has writers that cannot be completely fenced. It is
therefore foreground-only, but an explicit foreground activation may repair
preexisting divergence between its authority and required mirrors.

The repair path follows this order:

1. Read the authority and every required mirror.
2. If the authority is readable, use only its complete payload as the
   preservation baseline. Replace `_jackedAccountId` and `claudeAiOauth` with
   the requested account's managed values. Preserve every other top-level field
   from the authority.
3. Ignore preexisting required-mirror divergence for repair eligibility. Never
   import mirror-only fields or secrets into the prepared target.
4. Create a secret-free pending journal row before the native authority write.
   Because this mode has no recovery key, its before and target HMAC fields are
   empty and a crash can only recover as `indeterminate`.
5. Write the prepared target to the authority, then overwrite every required
   mirror with the same prepared payload.
6. Read the authority and all required mirrors again. Return
   `observed_target_unfenced` only when every store is readable and all
   canonical digests agree with the prepared target.

A missing authority is allowed only when all required mirrors are also
missing, in which case a foreground operation may create the topology. A
missing authority plus any readable required mirror returns `diverged` before
mutation. That mirror is not promoted to authority and is not erased. An
unreadable authority or required mirror returns `unusable` before mutation.

After mutation, a required-mirror write failure, unreadable store, concurrent
change, digest disagreement, or target mismatch remains `indeterminate`. The
engine does not claim preservation or success from partial post-write evidence.

Even after successful consensus, the result is `observed_target_unfenced`,
never `committed`. The repository sets the desired pointer and records an
observed-only journal outcome, but it does not advance the committed
`active_account_id` pointer.

The result reports `existing_sessions=restart_required` because a store
readback cannot prove what an already-running Claude process cached.

### Outcome contract

| Outcome | Claim |
| --- | --- |
| `committed` | Cooperative authority and required mirrors committed under the fence |
| `committed_degraded` | Commit succeeded; only optional metadata publication degraded |
| `observed_target_unfenced` | Target was observed across required stores, but concurrent writers cannot be excluded |
| `interactive_required` | The store requires a foreground authorization step |
| `interactive_operation_in_progress` | Another credential operation owns the process switch lease |
| `failed_preserved` | Failure occurred and the previous authority value was observed intact |
| `indeterminate` | The authority cannot be classified safely as before or target |
| `restart_required` | The requested background operation cannot safely activate an uncooperative topology |
| `unusable` | The request or store state cannot form a safe transaction |
| `unsupported` | A certified capability, complete writer evidence, or platform support is absent |
| `diverged` | Pre-write stores conflict in a mode that cannot repair safely, including a missing authority with a readable required mirror |

The model also reserves `busy` and `concurrent_write`. API clients
must treat every outcome as data rather than inferring success from a 2xx
transport response.

## 8. Foreground API and pending status

`POST /api/auth/accounts/{account_id}/use` is local-only. It requires a valid
page-session identifier and accepts separate action and operation IDs through:

- `X-Jacked-Page-Session`;
- `X-Jacked-Action-Id`;
- `X-Jacked-Operation-Id`.

The action ID is bound for ten minutes to the page session, operation ID,
action, and request digest. An identical retry replays the completed result. A
different request cannot reuse the ID. A concurrently claimed action returns
`CREDENTIAL_OPERATION_IN_PROGRESS` rather than starting a second mutation.

The response separates `storage`, `committed_authority`, `existing_sessions`,
and `provider_verification`. HTTP status maps as follows:

| HTTP | Outcomes |
| --- | --- |
| 200 | `committed`, `committed_degraded` |
| 202 | `observed_target_unfenced` |
| 428 | `interactive_required` |
| 409 | `interactive_operation_in_progress`, `busy`, `concurrent_write`, `diverged`, `restart_required` |
| 422 | `unusable`, `unsupported` |
| 503 | `failed_preserved`, `indeterminate`, and any unmapped non-success outcome |

`GET /api/auth/credential-operations/{identifier}` accepts either the action
or operation ID, is local-only and page-session-bound, and returns a secret-free
state. `complete` returns HTTP 200 and its stored result. `claimed` or another
nonterminal state returns HTTP 202 with `result: null`. An expired action is
reported as `expired`; the status endpoint does not extend its lifetime.

This outcome and status contract describes Claude accounts. The Codex branch of
the same `use` route calls the separate guarded Codex swap implementation and
reports that Codex must restart because it caches authentication at startup.

## 9. Session truth and existing sessions

Credential switching never bulk-relabels existing session rows.
`mark_global_sessions_pending()` changes only `observation_state` for open rows
whose `credential_scope` is already `global`. Scoped and unknown rows are left
alone, and the recorded account ID and email are not changed.

Hooks consume only `jacked-resolver-snapshot.json`; they do not read token
material. The snapshot has a fixed schema, a 30-second default freshness
window, owner-private file checks, and `desired` and `observed` identities.
The service-side observer refreshes it off the hook path and records no
credential revision for a passive read because a passive read is not a
transaction ordering witness.

On `UserPromptSubmit` or `Stop`, a hook opens a new session configuration span
only when it has a resolved, database-matched identity and a new credential
revision. Otherwise it updates activity or marks the observation unknown or
conflicting without changing the session's account label. This preserves the
history of what was actually observed instead of rewriting it to the latest
default.

## 10. Scoped launch limitation

`prepare_account_dir()` writes the selected per-account file, then must also
activate the certified global authority before starting Claude. For the shipped
build, a scoped file alone cannot establish runtime identity because the exact
certified topology is Keychain-first and global.

Consequences for `jacked claude <id>` today:

- an unknown or unsupported build aborts before Claude starts;
- a truthful activation must observe the selected account in the global
  authority and required mirror;
- the launch also changes the global credential stores used by future default
  sessions;
- already-running sessions keep their current credentials;
- the child gets `CLAUDE_CONFIG_DIR`, but inherited credential-scope, revision,
  nonce, and scoped-certification variables are removed first;
- the child gets `JACKED_CREDENTIAL_SCOPE=global` and the transaction revision
  only when the launch snapshot is fresh, schema-valid, resolved, global,
  stamped for the selected directory, and has a nonempty revision;
- otherwise the child gets `JACKED_CREDENTIAL_SCOPE=unknown`;
- `JACKED_SCOPED_CREDENTIAL_CERTIFIED` is deliberately not set.

Because scoped consumption is not certified, the launch is never labelled
`scoped`. The snapshot placed beside the launch directory is labelled `global`,
with evidence for the scoped-file input and global-authority observation. The
system must not claim that the child actually consumed only the scoped
credential file.

## 11. Foreign authority writes

Claude Code holds its OAuth token in memory. When that token expires, the
session refreshes it and writes the new payload into the same authority store
jacked writes. The payload has no `_jackedAccountId` stamp, and it carries a
`refreshTokenExpiresAt` field that jacked logs as schema drift. A session that
still holds an older account therefore replaces the account the user chose.

Three effects follow a foreign write:

1. The authority names a different account, so every session that reloads its
   credentials uses that account.
2. The unstamped payload makes the observed identity unusable, so the status
   line degrades.
3. Claude Code rotated the refresh token. If jacked overwrites the authority
   before it imports that token, the account loses its refresh lineage and the
   next refresh fails with `invalid_grant`.

`jacked/api/authority_guard.py` repairs this on each session observer pass,
before the pass observes the authority:

- A payload with a valid stamp is a jacked write. The guard does nothing.
- A payload without a stamp is a foreign write. The guard identifies it once
  per payload through the OAuth profile endpoint, and matches the email and the
  organization uuid to an account row.
- The guard adopts the rotated tokens into the identified row. It never imports
  a refresh token while the `invalid_grant` breaker is set for that row.
- The guard then reasserts the desired account through the transaction engine,
  with the `reassert` switch context. It reasserts at most once each 60
  seconds, and never while another operation holds the switch lease.
- When the identified account is the desired account, the guard still
  reasserts. That write restores the stamp, and no session changes account.
- An account the profile endpoint does not match is never overwritten. The
  guard logs a warning and stops, because jacked does not hold that account's
  refresh lineage.

The guard never raises. Each failure is logged and reported as a skipped heal.
The observer publishes the repair as snapshot evidence, for example
`authority:foreign-write:reasserted`.

Manual switches keep the same rule. Every truthful outcome, `committed`,
`committed_degraded` and `observed_target_unfenced`, records the active
account pointer and arms the auto-swap pause. On the shipped macOS topology
every switch reports `observed_target_unfenced`, so a rule that acted only on
`committed` left the pointer stale after each switch.

## 12. Safety invariants

1. Unknown executable or platform capability never permits mutation.
2. On the certified macOS topology, Keychain is authority and the global file
   is a required mirror. A file stamp cannot override Keychain disagreement.
3. A desired pointer is not a committed pointer.
4. `observed_target_unfenced` is not upgraded to `committed`.
5. Existing sessions are never relabelled merely because the default changed.
6. Background auto-swap must not use the global-uncooperative capability.
7. Pending recovery classifies by observation and never performs a repair
   write.
8. Resolver snapshots contain no token, secret, digest, HMAC, password, or
   backend locator fields.
9. Remote OAuth may save account data, but remote dashboards cannot activate
   local credentials.
10. Per-account launch directories remain inputs until exact scoped
    consumption is separately certified.

## 13. Primary implementation map

| File | Responsibility |
| --- | --- |
| `jacked/credentials/canonical.py` | Strict JSON parsing, canonical bytes, identity, digest |
| `jacked/credentials/capabilities.py` | Topology-keyed capability registry (platform, config mode, build floor) |
| `jacked/credentials/runtime.py` | Production capability assembly and activation entry points |
| `jacked/credentials/macos_store.py` | Keychain authority adapter driven by the signed security tool |
| `jacked/credentials/file_store.py` | Cross-platform durable JSON file adapter |
| `jacked/credentials/resolver.py` | Authority/mirror consensus and snapshot publication |
| `jacked/credentials/transaction.py` | Switch state machine and outcome axes |
| `jacked/credentials/recovery.py` | Observation-only pending-operation recovery |
| `jacked/web/credential_repository.py` | Secret-free SQLite transaction journal adapter |
| `jacked/api/routes/auth.py` | Local switch API, idempotency, status, active observation |
| `jacked/web/oauth.py` | OAuth storage and conditional local activation |
| `jacked/launch.py` | Per-account launch preparation and current scoped limitation |
| `jacked/api/session_observer.py` | Off-hook authority observation and snapshot refresh |
| `jacked/api/authority_guard.py` | Foreign-write adoption and desired-account reassertion |
| `jacked/data/hooks/session_account_tracker.py` | Snapshot-only session spans without relabelling |
