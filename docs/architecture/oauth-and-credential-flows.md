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

Credentials for Claude, Codex, and Grok stay on the one machine that runs
jacked. Jacked does not copy them to another machine, and it does not have to
survive two machines refreshing one login. The race that matters is two
processes on this machine: a Claude session and jacked, or the Codex or Grok
CLI and jacked. The live credential file or keychain on this machine is the
authority. A stored copy is refreshed only by reading that local credential
again, never by calling the token endpoint with the copy.

A Grok sign-in (Add Account or Re-auth) runs `grok login --device-auth` with
`GROK_HOME` set to a private folder (`<grok home>/jacked-accounts/.sign-in`)
and `--leader-socket` inside it, so the sign-in never replaces the live login
(`jacked/grok/login_import.py`). One sign-in runs at a time; a second request
gets 409 `GROK_LOGIN_IN_PROGRESS`. A watcher thread reads the CLI output to
its end and stops the CLI after 16 minutes (the device code lives 15). When
the CLI exits 0, jacked stores the login in the account slot. It writes the
live `auth.json` only when no usable login is live, or when the live login is
the same account. A re-auth refuses a login of a different account. The result
goes to the dashboard as the `provider_login_finished` WebSocket event, and
the private folder is removed.

Every account route that changes credentials runs the same remote-access gate
as `/use` (`_account_change_refusal` in `jacked/api/routes/auth.py`): Codex
and Grok add, Grok re-auth, token refresh, edit, reorder and delete. The
Claude OAuth add and re-auth only store a login that the user finished; the
gate result decides whether the flow may activate it.
`tests/unit/test_account_change_gate.py` fails for a new write route that is
neither gated nor listed as exempt.

Each Claude account row keeps two token sets:

| Fields | Consumer | Publication rule |
| --- | --- | --- |
| `access_token`, `refresh_token`, `expires_at` | Jacked profile, usage, and API-key flows | Database state; the primary refresh token is not handed to Claude Code as a fallback refresh token |
| `cc_access_token`, `cc_refresh_token`, `cc_expires_at` | Claude Code | Used to build the credential payload when present |

`build_oauth_data()` prefers the Claude Code token set. If it must fall back to
the primary access token, it publishes a null `refreshToken` so Claude Code
cannot rotate Jacked's primary refresh token. It never publishes the refresh
token of a dead Claude Code chain (section 2.1). A dead chain that still holds
an unexpired, recovered access token publishes that token with a null
`refreshToken`. Otherwise the payload falls back to the primary access token.

### 2.1 Refresh chains

Each token set is a separate refresh lineage, called a chain: `primary` and
`cc`. Each chain has its own circuit breaker:

| Chain | Breaker columns |
| --- | --- |
| `primary` | `refresh_failure_type`, `refresh_last_failed_at` |
| `cc` | `cc_refresh_failure_type`, `cc_refresh_last_failed_at` |

A failure or a success on one chain never changes the other chain's tokens or
breaker. The guards that protect the Claude Code lineage read only the `cc`
breaker. These guards are the reconcile refresh-token import, the launch token
sync, the authority guard import, and `build_oauth_data()`.

Each refresh mode names one chain. `_refresh_token_flow()` uses that chain
under the lock of that chain. It uses the chain for the columns it writes, for
the `invalid_grant` branch, for the log text, and for the token it returns.

The dead-chain rule:

- An `invalid_grant` on the `cc` chain kills the chain. The flow removes
  `cc_refresh_token` and records `invalid_grant` on the `cc` breaker.
- The flow reads the live store when the chain dies. The shared ownership
  rule (section 11) must prove that the live tokens belong to the row.
- A live refresh token is new when it is a non-empty string, it is not the
  failed token, and it is not the row's primary refresh token. Its
  fingerprint must also not be in `cc_refresh_failed_token_fp`.
- The row adopts a new live refresh token as its `cc` lineage. One
  compare-and-swap write stores that token, its pass-through fields, the
  access token and its expiry. The same write clears the `cc` breaker.
- Any other live refresh token gives the row only the live access token and
  its expiry. The `cc` breaker keeps `invalid_grant`.
- Reconcile and the launch token sync apply this rule through
  `dead_chain_adoption()` in `credential_helpers.py`. When the `cc` chain
  dies, the rule reads the failed token as `cc_refresh_token`. It reads the
  stored fingerprints as they are. Thus the rule refuses the failed token
  and each `cc` token that died before it.
- jacked does not exchange an adopted refresh token while a session can hold
  it. `cc_rotation_block()` refuses that exchange (section 11).
- Otherwise the flow also removes `cc_access_token` and `cc_expires_at`. No
  consumer can then send a dead token.
- The flow adds the fingerprint of the failed `cc` refresh token to
  `cc_refresh_failed_token_fp`. It also does this when it adopts a new live
  lineage, and when a re-authorization stored a new `cc` lineage before the
  clean-up ran: the clean-up keeps the new lineage and still records the
  token that died. The column keeps the fingerprint of each condemned `cc` token. The
  newest fingerprint is first, and a space separates two fingerprints. A dead
  token stays dead, so the flow never removes an earlier fingerprint.
- No importer adopts a `cc` refresh token with one of these fingerprints.
  This applies to a dead chain and to a live chain, because a cleared
  breaker does not release a condemned token. Reconcile, the launch token
  sync and the authority guard use `refresh_token_importable()` in
  `credential_helpers.py`. An importer that refuses a condemned token also
  takes none of its pass-through fields: they describe the refused lineage,
  and the row keeps its own.
- The `cc` flow never sends a condemned `cc` refresh token. When an importer
  brought one back, the flow runs the `invalid_grant` clean-up with no
  request. jacked logs `Account N: not sending the Claude Code refresh token;
  an earlier invalid_grant condemned it (caller cc)`.
- An `invalid_grant` on the `primary` chain records the failure on the
  `primary` breaker only. The `cc` tokens do not change.
- A new Claude Code authorization clears the `cc` breaker. A primary
  re-authorization clears the `primary` breaker.
- A background auto-swap never selects an account with a dead `cc` chain.
- The flow records the fingerprint of the failed primary refresh token in
  `refresh_failed_token_fp`. The verdict is the fingerprint alone. The
  breaker columns do not change it, so a writer that clears the primary
  breaker does not release the token.
- While the row holds a refresh token with that fingerprint, no path sends
  the token. The flow refuses the exchange and logs one line with the
  account id and the caller. The row shows the error `Sign-in required: the
  refresh token was revoked`.
- Only a new stored refresh token releases the primary verdict. A database
  trigger clears the fingerprint for each writer: a re-authorization, an
  imported rotation, or a new add of the account.

`build_oauth_data()` never publishes the refresh token of a dead chain. A
writer must not put that null refresh token over a working refresh token of
the same account. Section 7 describes the refusal.

The token chooser:

- Every consumer that picks a token for a Claude account calls
  `choose_usage_token()`.
- The chooser returns a live `cc` token first. A `cc` token is live when it
  is present, it has not expired, and its breaker records neither
  `invalid_grant` nor `auth_rejected`.
- Else the chooser returns the primary access token. Else it returns nothing.

A rejected token renews only the chain that owns it:

- A 401 or 403 on the row's `cc` access token rotates the `cc` chain, but only
  for an account that is not active. The active account's `cc` refresh token
  belongs to the running Claude Code sessions, and jacked never rotates it.
- When no rotation is possible, the flow records `auth_rejected` on the `cc`
  breaker and the retry uses the primary token. The retry never takes the
  primary circuit-breaker path.
- A newer `cc` access token from reconcile or the authority guard clears
  `auth_rejected`.
- A 401 on the primary token renews the primary chain through the circuit
  breaker.
- A 429 backs off. The backoff honors `Retry-After` up to 6 hours. Otherwise
  it uses the escalating per-account step.
- A 429 on the `cc` access token never rotates a chain. A `cc` rotation uses
  a single-use refresh token that a running session can hold. The flow logs
  one line, and the caller backs off.
- A 429 on the primary token can rotate the primary chain. This rotation is
  allowed only after 3 consecutive 429s on the same token. It is allowed at
  most once in 3 poll periods (the `usage_check_interval` setting, at least
  600 seconds), read at call time.

Token exchange outcomes:

| Failure type | Meaning | Cooldown |
| --- | --- | --- |
| `not_sent` | The request did not reach the server (DNS, connect, or pool timeout). The refresh token is intact. | 60 s |
| `reply_lost` | The request was sent, but the reply was lost (read or write timeout, dropped connection, unreadable 200 body). The server can have consumed the refresh token, so the chain is suspect. | 900 s |
| `network_error` | Any other transport failure. | 60 s |

The heal sweep does not clear a network breaker while its cooldown runs. It
probes the token endpoint host once for each pass, with a 5 second HEAD
request, before it forces a renewal. When the probe fails, the sweep defers
the account to a later pass.

The heal sweep never clears an `invalid_grant` breaker. When the fingerprint
matches the stored refresh token, the sweep does not send the token. It logs
one line for the account in each pass. Then it validates the access token
with a profile request. An access token that works heals the account.
Otherwise the account counts as `confirmed_invalid`. A breaker from an older
version has no fingerprint. That account gets one exchange, and the exchange
records the fingerprint. A row whose last error says that its token belongs
to a different account gets no exchange either: the validation leg keeps it
invalid until a re-authorization replaces the token.

The sweep reads each row one time in each pass. Before it forces a renewal,
it clears a breaker that is not `invalid_grant`. This write is a
compare-and-swap on `refresh_token`, `refresh_failure_type`,
`refresh_last_failed_at` and `refresh_failed_token_fp`, as the sweep read
them. Another writer can record a verdict or store a new refresh token while
the sweep waits for the probe. Then the write does not occur, the sweep sends
no request, and the account goes to the next pass. jacked logs `Account N:
heal deferred; the refresh state changed while the heal checks ran`.

Validation sends one profile request with the access token. A 200 proves that
the token works. It does not prove that the token belongs to the row. When
the profile names another account (another email, organization, or account
uuid), validation applies nothing from the profile. It marks the row
`invalid` with the error `Token belongs to a different account`. It writes
this mark only while the row holds the token that it checked. The `valid`
mark follows the same rule: validation records it only while the row holds
the access token that it checked. When a re-authorization stored another
token meanwhile, validation records nothing and asks for a new check. A
profile with no owner value (no account object or no email) is not evidence
of another owner. While `foreign_token_fp` binds the row's primary refresh
token, such a profile proves no owner: the verdict stands, and validation
does not mark the row valid. The request after a token renewal uses the same
rule.

The mark also binds the verdict to the row's primary refresh token:
`foreign_token_fp` holds the fingerprint of that token. While the row holds a
refresh token with this fingerprint, its primary tokens belong to another
account. No path sends that refresh token, because the exchange spends a
single-use token of the other account under this row. No usage request
sends the access token beside it, so the usage of another account never
goes into this row. The usage request uses the row's own Claude Code token,
or it does not run. A usage success through the row's own Claude Code token
does not mark the row valid while the verdict stands. The refresh flow
refuses the exchange and logs one line with the account id and the caller.
The heal sweep skips the exchange and logs one line in each pass. Then the
profile check keeps the row `invalid`. The verdict is the binding alone. A
usage success, the watchdog and other error writers can change the error
text or the status, but they do not release the verdict. A path that writes
an error after a failed refresh or validation keeps the text of the verdict.
Only two events release the verdict: a new stored refresh token (a database
trigger clears the fingerprint), or a validation that proves the row's own
account. That validation clears the fingerprint only while the row holds the
access token that it checked. A row that carries the text of the verdict
with no fingerprint (a mark from before the column) is bound one time when
the schema opens, before any refresh path runs.

No publisher writes the primary tokens of a bound row to a credential store.
`build_oauth_data()` in `jacked/api/credential_helpers.py` builds the payload
for every publisher. It refuses (`ForeignPrimaryTokens`) when
`foreign_token_fp` binds the row's primary refresh token and the row has no
usable Claude Code chain of its own. Each publisher then fails closed. The
switch engine answers `unusable` with code `FOREIGN_PRIMARY_TOKENS` before it
resolves the platform, and its preflight refuses inside the lease when a
validation bound the row after that check. The auto-swap skips the target and
logs one line. The launch stops with the message of the use route, in the
resolver and again before it writes the per-account file. A re-stamp
publishes the authority's own payload, so the verdict does not apply to it.
The window keeper sends no bound primary token: a ping is a usage request,
and it would open the other account's window under this row. A bound row with
a usable Claude Code chain publishes that chain, which is the row's own
lineage.

Each write that validation makes after a request is a compare-and-swap on
the access token that the request sent. Thus a re-authorization that lands
during the request keeps its own status. This applies to a 401 or 403, to a
5xx or 429 answer, to a timeout, and to an error. When the row identity
changes during the check, validation records nothing. The result is `The
account changed during validation. Validate the account again.`, and the
heal sweep counts the account as `deferred`. A profile for a row that is
gone records nothing.

The background refresh loop counts only real token exchanges as `refreshed`.
A token inside the 4 hour buffer but outside the 5 minute refresh window is
`skipped`.

A one-time migration moves a legacy shared breaker to the `cc` columns. The
old code handled a dead `cc` chain by clearing only `cc_refresh_token`, so the
migration moves a row with all of these values:

- `refresh_failure_type` is `invalid_grant`;
- `cc_refresh_token` is null and `cc_access_token` is present;
- `refresh_token` is present;
- `cc_refresh_failure_type` is null.

A marker row (`cc_breaker_split`) in the private `schema_migrations` table
gates the move. The marker is not a setting, so the settings API cannot list,
change, or delete it. The move
does not depend on the pass that adds the columns, so a database that got the
columns without the move still gets it. The move and the marker write share
one transaction. After the move, the shared pair belongs to the primary chain
only. Thus the move runs once for each database and never reclassifies a later
primary failure. A moved row whose `cc` access token has expired also loses
that token.

OAuth completion saves the account in SQLite before any local activation. It
attempts local credential activation only when the new or re-authorized account
should be the default and the requesting client passes the credential-mutation
scope gate in section 8 (loopback always; a remote client while remote access is
on and its address is inside the enabled scope). For a client that does not,
`_complete_auth()` saves the account, skips host credential mutation, and puts
`activation_status=local_only` in its internal completion result.

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

### Pass-through fields

Claude Code writes four fields into `claudeAiOauth` that jacked's columns do
not model: `refreshTokenExpiresAt`, `clientId`, `subscriptionTypeSource` and
`subscriptionTypeVerifiedAt`. jacked keeps them per account in
`accounts.cc_oauth_extra` (`jacked/credentials/oauth_extra.py`) and writes
them back on the next switch. Two rules bind the stored values:

- The record names the refresh lineage it was captured with (`_lineage`).
  jacked reads it back only while the row still holds that refresh token.
- The record names the tier that `subscriptionTypeVerifiedAt` verified
  (`_tier`). jacked writes the timestamp back only while the row's
  `subscription_type` still equals that tier. A changed tier publishes
  `subscriptionTypeSource` with no timestamp. Claude Code applies the same
  rule in its own refresh merge, and it re-checks the profile for that shape.

jacked never invents a value. Before a switch, the transaction engine compares
the keys in the authority with the keys in the target payload. It logs a
WARNING for a key outside this allowlist, because the switch drops that key.
It logs an allowlisted key that the target payload does not carry at DEBUG,
because Claude Code re-derives that key. The DEBUG line names the account id
and the key names. Neither line contains a value.

jacked's own refresh of the Claude Code chain applies the same rules:

- The refresh binds the stored fields to the new refresh token.
- The refresh takes `refreshTokenExpiresAt` from the token response when the
  response sends it. Otherwise, it keeps the previous value.
- The refresh does not change the tier. It keeps `subscriptionTypeVerifiedAt`
  only while the row's `subscription_type` equals the captured tier.
  Otherwise, it removes the timestamp and keeps `subscriptionTypeSource`.

After an invalid_grant, jacked can keep a live access token that it proves
belongs to the account. When it adopts a live refresh token, it binds the
pass-through fields to that token, `refreshTokenExpiresAt` included. When it
keeps only the access token, it stores the pass-through fields with no
refresh token and does not keep `refreshTokenExpiresAt`. It binds
`subscriptionTypeVerifiedAt` to the tier in the live object.

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
limit, then as escaped JSON with `-w` when that form fits. Otherwise the write
passes the hex payload as a process argument and warns once per process. Claude
Code writes the same item this way, and the item grows with the OAuth tokens of
each MCP server (`mcpOAuth`). Set `JACKED_KEYCHAIN_ARGV_FALLBACK` to `0`, `false`,
`no` or `off` to refuse
the argument write. That refusal stops every account switch on a large item. Reads decode the
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

jacked replaces a file only through the shared atomic writer in
`jacked/atomic_io.py` (#237i). On Windows, a replace fails while another
handle holds the target file: a reader, an indexer, or an antivirus scan. The
shared replace then retries for up to 1 second, and after that it reports the
error. The Claude config writer (section 5.5) allows 3 seconds. A test fails
when a module in `jacked/` uses `os.replace` directly.

A compare-and-swap writer checks its precondition again before every retry,
because another writer can change the target during the pause. If the file
changed, `FileCredentialStore` refuses the write as a concurrent write. The
Claude config writer stops with `cas_aborted`, and the Task Scheduler
supervisor stops the rename of the legacy startup script. When another handle
also refuses the check, the writer counts one more refused try. The check and
the replace use the same retry time.

When a caller asks for a mode (0o600 for a credential file), the shared writer
sets it on the open temp file before the bytes land. The mode goes through the
file descriptor, so a link put at the temp name cannot redirect it. On Windows
a mode bit does not control who can open a file. So the shared writer also
gives the temp file a private ACL for the current user. The ACL goes through
the temp file's name (and so does the mode on a Windows build without
`os.fchmod`): a process that can put a link at that name can already read the
folder's files, so the name is not a new exposure. If the writer cannot set the
mode, it does not publish the file. The per-account launch files are the
exception for the mode bits only: they log a refused chmod and continue,
because some storage cannot carry mode bits. A refused Windows ACL stops every
writer, because on Windows the ACL is the only protection the file has.

The dashboard routes that write into a project (`.claude/settings.local.json`
and `lessons.md`) check that the path is not a link to a location outside the
project. The check runs twice: once in the route, and once more in the worker
right before the write, so a link made between the two is refused as well.

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

### Refresh downgrade refusal (#149i)

- Before the pending row and before any write, the engine asks the request's
  `refresh_downgrade_check`. It asks inside the switch lease, after it reads
  the authority.
- The check is `refuses_refresh_downgrade()` in
  `jacked/credentials/refresh_guard.py`. It refuses the write when all three
  conditions are true:
  1. The target payload has no refresh token: null, empty or absent.
  2. The authority holds a refresh token that is a non-empty string.
  3. No positive evidence gives the authority tokens to another account. The
     shared ownership rule, which does not require a stamp, proves the owner
     when it can.
- Before the rule costs a profile request, the writer asks the database which
  row stores the authority's refresh token (`account_id_for_refresh_token()`
  in `jacked/web/database.py`, passed as `lineage_known`). A refresh token is
  issued to one account. The target row: the write is refused. Another row:
  the write goes ahead. No row, or a failed lookup: the ownership rule
  decides. A primary refresh token that `foreign_token_fp` binds names no
  row. The engine preflight, the auto-swap request and the launch's
  scoped-file check pass the finder.
- An empty store, a row that stores the refresh token for another account, a
  200 that names another owner, and a payload whose expiry is not a real time
  do not stop the write. Nothing else does. A failed profile request, an
  expired access token, a 401 and a 403 prove no owner. A stamp or a
  per-account directory that names the target then keeps the pair with the
  final refusal below. Any other payload is refused with `busy` and code
  `OWNERSHIP_UNCHECKED`: a transient refusal that every caller retries. A
  refusal is retryable, a lost refresh lineage is not. The launch also replaces
  a scoped file on the digests that an earlier session remembered for it.
- A refusal writes nothing. The engine creates no pending row, writes no
  store, and leaves the authority byte-identical.
- The result is `unusable` with code `CC_REAUTH_REQUIRED`. Its message is
  `not publishing a null refresh token over a live one (cc re-auth required)`.
- The engine logs one WARNING line with the account id and that message. The
  line holds no token value.
- `activate_account()` builds the check for the object the request
  publishes: the row's pair, or the authority's own object for a re-stamp.
  The preflight builds it again inside the lease, for the object it keeps or
  rebuilds. The auto-swap request carries the same check.
- Usually the preflight reconcile adopts the live lineage first (section 11),
  and the write then publishes it. The refusal protects a row that the
  adoption cannot repair. An example is a row that stores the live refresh
  token while its breaker still records `invalid_grant`.
- The switch route answers the refusal with HTTP 422. The body holds the
  message and the code. The dashboard shows a notice that names the fix: a
  new Claude Code authorization for that account.

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
| `unusable` | The request or store state cannot form a safe transaction. Code `CC_REAUTH_REQUIRED` means the write would put a null refresh token over a live one. Code `FOREIGN_PRIMARY_TOKENS` means the row's primary tokens belong to a different account and the row has no Claude Code chain to publish |
| `unsupported` | A certified capability, complete writer evidence, or platform support is absent |
| `diverged` | Pre-write stores conflict in a mode that cannot repair safely, including a missing authority with a readable required mirror |

The model also reserves `busy` and `concurrent_write`. API clients
must treat every outcome as data rather than inferring success from a 2xx
transport response.

## 8. Foreground API and pending status

`POST /api/auth/accounts/{account_id}/use` is scope-gated. A loopback client
(`127.0.0.1`, `::1`, `localhost`, and Starlette's `testclient`) may always call
it. A remote client may call it only when the persisted remote-access setting
is enabled *and* its address is inside the enabled scope:

- scope `all`: any address that parses as an IP;
- scope `tailscale` (also the fallback for an absent or unrecognized scope):
  IPv4 inside Tailscale's CGNAT range `100.64.0.0/10`. The IPv6 ULA prefix
  `fd7a:115c:a1e0::/48` is accepted too, but defensively: the tailscale-scope
  bind is IPv4-only today, so no live client arrives from it.

A client host that is missing or does not parse is denied unless it is a
loopback literal. No settings DB, and a settings DB that raises on the read,
both mean loopback only: the reader fails closed rather than propagating, since
it runs on the polled `/active-credential` too. Denied requests get HTTP 403
with code `CREDENTIAL_MUTATION_LOCAL_ONLY` and a message chosen by the reason
(`remote_access_off` or `outside_scope`).

`request.client.host` is the socket peer, or the address a trusted loopback
proxy forwarded. All three uvicorn launch sites pin `proxy_headers=True,
forwarded_allow_ips="127.0.0.1"` explicitly rather than inheriting
`FORWARDED_ALLOW_IPS`: `tailscale serve` proxies from loopback and rewrites
`X-Forwarded-For` to the real tailnet source, so honoring a loopback proxy is
what stops a serve viewer from inheriting loopback's unconditional permission,
and pinning the trusted set stops the environment from widening it.

Accepted remote switches are rate limited per client host (6 per rolling 60
seconds, 429 `CREDENTIAL_SWITCH_RATE_LIMITED` with `Retry-After`), evaluated
after the gate so a denied client never consumes budget. Loopback is not
metered. Every non-loopback decision, allowed or denied, leaves one INFO line
naming the client, and a remote switch is labeled with its client address in
the swap broadcast that drives Swap History.

The setting that drives all of this is itself loopback-only to write:
`PUT /api/settings/remote-access` and `POST /api/settings/remote-access/restart`
refuse a non-loopback client with 403 `REMOTE_ACCESS_SETTINGS_LOCAL_ONLY`, and
the GET reports `editable` so the dashboard can render the controls read-only.
Without that, a remote peer could grant itself credential control by writing the
bit, and turning remote access off would not revoke anything until the socket is
rebound by a restart. `POST /api/upgrade` is deliberately NOT gated: the
remote-access confirmation dialogs promise remote upgrades explicitly. The policy itself lives in
`jacked/api/remote_access.py` (`credential_mutation_allowed`) and reads the
same `remote_access_enabled` / `remote_access_scope` settings the bind planner
uses, so enabling remote access is the single switch that grants credential
control, which is what both remote-access confirmation dialogs promise.

The route also requires a valid page-session identifier and accepts separate
action and operation IDs through:

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
or operation ID, is page-session-bound, is gated by the same scope policy (403
`CREDENTIAL_STATUS_LOCAL_ONLY` when denied), and returns a secret-free
state. `complete` returns HTTP 200 and its stored result. `claimed` or another
nonterminal state returns HTTP 202 with `result: null`. An expired action is
reported as `expired`; the status endpoint does not extend its lifetime.

`GET /api/auth/active-credential` publishes the same decision for the calling
client as `allow_credential_activation` (a required field on the response
model) plus `credential_activation_reason` (`remote_access_off`,
`outside_scope`, or null) on every response path, including the unresolved and
conflicting ones. The dashboard reads it to disable the Use
Account button and show one hint, rather than offering a control the server
will reject. The field defaults to `true`, so a client talking to a server that
predates it behaves as before; the server, not the flag, remains the gate.

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
- on a Keychain build, the engine can refuse the global activation with code
  `CC_REAUTH_REQUIRED`. When the authority still names the selected account,
  the launch continues: the authority holds that account's own working
  tokens, and Claude starts with them. The launch prints
  `Note: account N: <message>. Claude starts with the current credentials of
  this account.` to stderr, and the snapshot evidence is
  `launch:global-authority:live-refresh-kept`. Any other refusal stops the
  launch, as before;
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

### Token sync from the per-account file

While a launched Claude Code runs, `jacked/launch.py` `_sync_tokens_from_file()`
copies a rotated token pair from the per-account `.credentials.json` into the
account row. The sync runs when the mtime of the file changes. The thread reads
the mtime every 30 seconds. The sync also runs one time when Claude Code stops.

The per-account `.claude.json` does not prove who owns the tokens, because
jacked and Claude Code both write it from the token that a process holds. The
sync does not read that file. The sync uses the import rules of reconcile
(`jacked/api/credential_helpers.py`):

- An expiry that is not a real time stops the sync. jacked logs
  `Token sync BLOCKED for account N: unreadable expiry`.
- A row that has no Claude Code token gets no tokens. Only the OAuth flow or a
  jacked refresh fills those columns.
- jacked does not import its own primary-token fallback into the cc_* columns,
  because that merges the two chains.
- A pair that expires before the pair in the row does not replace that pair.
  The exception is a row token that is known to be dead (#179i).
- A dead chain (`invalid_grant`) follows the dead-chain rule of section 2.1.
  A new refresh token in the file is adopted, and the same write clears the
  `cc` breaker. The failed token, an empty token or the row's primary refresh
  token gives only the access token and its expiry, and the breaker keeps
  `invalid_grant`. The rule also applies when the row already holds the
  file's access token: the sync then takes no pass-through fields for the
  failed token. An adoption logs `Account N: adopted the live Claude Code
  refresh lineage after invalid_grant (ownership proven)`. The sync reads
  `cc_refresh_failed_token_fp` with the row. Thus the rule refuses the failed
  token also after the clean-up removed it from the row. A database from
  before the fingerprint column syncs as before.
- A live chain never takes a refresh token whose fingerprint is in
  `cc_refresh_failed_token_fp`. The file then gives the access token and its
  expiry only, and the row keeps its own refresh token. jacked logs `Account
  N: the per-account file holds a Claude Code refresh token that an earlier
  invalid_grant condemned; only the access token is imported`.
- The shared ownership rule (`live_payload_ownership`) must prove the owner.
  The per-account file has no `_jackedAccountId` stamp, so the rule does not
  require one. A token that the row holds proves the owner with no request.
  Any other token gets one cached profile request, and the shared owner rule
  compares the profile with the row. A profile answer of 401 or 403 is about
  the token, so it settles the verdict as refused. The verdict cache keeps
  that refusal for 15 minutes, and then the next sync asks again, because an
  edge or a policy can refuse a token for a short time. A profile that names
  another owner is final for the life of the token.

The sync runs the local rules first. Only a token that they cannot place costs
a profile request. The sync gives one of four results:

- `imported`: the tokens belong to the row, and the row holds them now.
- `refused`: the profile names another owner, an earlier session remembered
  that verdict, or a local rule refuses the tokens (an expired or refused
  access token with no new refresh token, or an expiry that is not a real
  time). jacked writes nothing and logs
  `Token sync BLOCKED for account N: ownership not proven`. The line gives the
  row id only. A refusal holds only for the row it was decided against: when
  the row identity (email, organization uuid, account uuid) changed during
  the profile request, the sync defers instead, and the next pass decides
  against the new row. The verdict cache is keyed by that identity, so the
  new row gets a fresh profile request. A refusal the profile settled by
  naming another account is also remembered beside the per-account file,
  as digests of the pair and of the row identity, never a token
  (`.jacked-foreign-pair.json`). The next launch replaces that pair without
  a request, even when its access token expired. The launch removes the
  marker when it writes the row's pair.
- `deferred`: the profile request failed, an error stopped the sync (for
  example, a half-written file), the profile endpoint answered 401 or 403
  beside a new refresh token (that answer names no owner, and an edge or a
  proxy can give it), or the file holds a rotation whose access token expired
  before the sync could ask (a new refresh token beside an expired access
  token). Nobody can prove that rotation now, and the file keeps it for the
  next session. jacked writes nothing. The poll loop keeps the old mtime, so
  the next poll tries again when the file does not change. The verdict cache
  keeps the result of a failed request for 5 minutes and a 401 or 403 for 15
  minutes, so the next request goes out after that time. The first deferral of
  a row in a process is a WARNING, and the retries are INFO lines.
- `unchanged`: there is nothing to copy, or another writer changed the row
  first. jacked writes nothing.

The write is a compare-and-swap on the access token that the sync read before
the profile request. A writer that changes the row during the request wins.

These steps keep a rotation that the loop could not prove from being lost:

- When Claude Code stops, jacked clears the verdict cache before the exit-time
  sync, so a deferred token gets one fresh profile request. A refused
  exit-time sync logs a WARNING with the row id, because the next launch
  writes the row's tokens over the file.
- The sync thread syncs the file one time when it starts, before it accepts
  the file's mtime as processed, so a rotation that landed between the
  launch's own write and the thread's start is not missed.
- Before the next launch of the same account, `prepare_account_dir()` runs the
  sync on the existing per-account file before the pre-launch refresh spends
  the row's refresh token. A proven rotation goes into the row first, and the
  launch then re-reads the row. A refused result falls through, and the launch
  writes the row's tokens over the file, with a WARNING that gives the row id
  and the sync result. Only a refused result logs that WARNING: a routine
  launch (the row is newer than the file, or the file holds the primary
  fallback) replaces the file quietly. A deferred result over a pair that is
  not the row's keeps the file's tokens for that launch and logs a WARNING,
  because the row's spent lineage written over the only live rotation would
  be the real loss. A deferred result over the row's own pair (a locked
  database) keeps nothing: the pre-launch refresh runs, and the launch writes
  the row's pair. The launch also skips its pre-launch Claude Code refresh when
  it keeps a rotation, because the rotation in the file may have spent the
  refresh token of the row. Claude Code refreshes from the file,
  and the sync proves the new access token on its next poll. On a platform
  whose authority is the launch directory's own file, the launch reads back
  the pair it wrote, so a kept rotation reads back as itself, and the
  resolver snapshot carries the evidence
  `launch:scoped-file:unchecked-rotation-kept`.
- The write of a proven rotation is a compare-and-swap on the access token AND
  on the identity the proof was made against (email, organization uuid,
  account uuid). A writer that changed either during the profile request wins,
  and the sync forgets its cached verdict for that row, so the next attempt
  proves the token against the new identity. The write also compares
  `cc_refresh_token`, `cc_refresh_failure_type` and
  `cc_refresh_failed_token_fp`, because the dead-chain rule reads them. Thus
  a chain that dies during the profile request never gets a refresh token
  beside a standing `invalid_grant` (#244i). A token that an `invalid_grant`
  condemns during the request is not adopted either.
- The launch never writes a null refresh token over a live one of the same
  account (#149i). A dead-chain row publishes no refresh token. When the
  per-account file holds a working refresh token of the same account, the
  launch keeps the file's tokens, logs `Account N: not publishing a null
  refresh token over a live one (cc re-auth required)`, and starts Claude.
  The shared ownership rule proves the owner; the file has no stamp, so the
  rule does not require one.

## 11. Foreign authority writes

Claude Code holds its OAuth token in memory. When that token expires, the
session refreshes it and writes the new payload into the same authority store
jacked writes. The payload has no `_jackedAccountId` stamp. It carries the pass-through
fields that jacked stores per account (see section 3); a key outside that
allowlist is logged as schema drift. A session that still holds an older
account therefore replaces the account the user chose.

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
- The guard adopts the rotated tokens into the identified row, through the
  shared ownership rule below. It never imports a refresh token while the
  `cc` breaker records `invalid_grant` for that row.
- The guard then reasserts the desired account through the transaction engine,
  with the `reassert` switch context. It reasserts at most once each 60
  seconds, and never while another operation holds the switch lease.
- When the identified account is the desired account, the guard still
  reasserts. That write restores the stamp, and no session changes account.
- An account the profile endpoint does not match is never overwritten. The
  guard logs a warning and stops, because jacked does not hold that account's
  refresh lineage.
- When the desired account cannot be reasserted (its row is missing, disabled,
  invalid, or has no Claude Code refresh token), the guard re-stamps the
  authority for the account that holds it, with the same tokens Claude Code
  wrote. Sessions do not change account, the observed identity becomes usable,
  and the status line shows the true account. The guard cannot restore a
  refresh lineage that is already lost; the user re-authenticates the desired
  account to make it the default again. When the holding account has no
  Claude Code access token either, there is nothing to re-stamp with: the
  guard settles the payload at once, logs one WARNING, and leaves the
  authority unstamped.
- Both repairs, the reassert and the re-stamp, share one retry rule
  (#157i). The guard records a repair as done only after the engine commits
  it. A 60-second floor spaces the attempts. The transient answers of the
  engine (a held switch lease, a busy outgoing row, a torn authority, an
  unreadable authority) are retried after the floor and do not count; ten
  of them in a row are reported with one WARNING. Any
  other failure is attempted three times. The third failure settles the
  payload with one WARNING that names the row and the outcome, and the guard
  tries again only when the authority payload changes.

A stamp does not prove who owns the tokens. Claude Code keeps the top-level
`_jackedAccountId` when it rewrites the item. A `/login` to another account in
one session writes that account's tokens under the old stamp (seen on macOS
on 2026-10-17). A session refresh also keeps the stamp. Thus a rotation by a
running session never makes an unstamped payload, and the unstamped-payload
path alone never adopted one.

The guard therefore also examines a stamped payload:

- When the stamped row already holds the payload tokens, the guard does
  nothing and sends no request.
- When the payload holds the row's primary access token, the payload is the
  primary fallback of `build_oauth_data()`. The guard never imports it into
  the `cc` columns.
- Otherwise the guard identifies the payload once through the profile
  endpoint. It adopts the tokens into the row that the shared ownership rule
  proves. That row can differ from the stamp.
- When the owner differs from the stamp, the guard logs a warning. Then it
  reads the `oauthAccount` block of `~/.claude.json` (#154i). The block is
  evidence of the account that the user chose. It does not prove who owns
  the tokens.
  - When the block names the owner, the user chose that account with
    `/login`. The guard makes the owner the desired and the active account
    in one settings write, arms the auto-swap cooldown and marks the open
    sessions pending, as a manual switch does. Then it re-stamps the
    authority for the owner with the same tokens. An owner that jacked has
    disabled or deleted is not followed: the guard adopts the tokens, logs
    one WARNING, and leaves the pointers alone.
  - When the block names the stamped account, a stale session refreshed the
    tokens of another account. The guard reasserts the stamped account when
    it is also the desired account and the guard can reassert it.
  - When the block is missing or unreadable, or names neither account or
    both accounts, the guard only adopts the tokens. It logs one INFO line
    with the reason.
- The block names a row by the account uuid when the row records one, and by
  the email otherwise. The organization uuid must also match when both sides
  record it, because the rows of one person in two organizations share one
  account uuid.
- Claude Code 2.1.316 writes the identity keys of `oauthAccount` at `/login`
  before it saves the new tokens. A token refresh merges only display fields
  into the block. Thus the block names the new account after a `/login`, and
  a stale refresh does not change it.
- The re-stamp and the reassert use the same floor and settle rule as the
  repairs of an unstamped payload.
- A re-stamp republishes exactly the payload that the authority holds, with
  the stamp of the owner. It adds the stamp and the config identity, nothing
  else, so a row that cannot publish the live pair (for example, the `cc`
  breaker kept a condemned refresh token out of the row) is still re-stamped
  without a loss. The request names the digest of that payload. When the
  authority holds another payload by the time the engine reads it inside the
  lease (Claude Code rotated meanwhile), the engine refuses with
  `concurrent_write` and writes nothing, and the guard adopts the new payload
  on its next pass. This rule also applies to the re-stamp of an unstamped
  payload.
- After a committed repair, the guard keeps the 16-character fingerprint of
  the refresh and access tokens that the engine reports it published
  (`SwitchResult.published_fingerprint`, from the exact payload written) in
  the internal setting `_last_authority_write_fp`, which the settings API does
  not show. A stamped payload with these tokens is jacked's own write, so the
  guard does not examine it again.

The identity watcher refuses to publish an identity for a stamped payload
whose token belongs to a different account. Its stamp-mismatch evidence
describes only the account that the authority holds now. The evidence stops
when a re-stamp or a reassert makes the stamp and the tokens agree. The
watcher also skips a pass for 15 seconds after the identity keys of the
config change in a file written inside that time, because a `/login` writes
the identity a moment before it saves the tokens, and a republish in that
window would undo the user's choice. Other writes to the file do not count,
because every running session rewrites it. A skipped pass spends no attempt
floor and keeps an earlier sticky reason.

`live_payload_belongs_to()` is the one shared stamp and ownership rule. Four
paths use it: reconcile, the authority guard import, the refresh downgrade
check (section 7), and the `invalid_grant`
live recovery. When the caller requires a stamp, the stamp must name the row.
A token that the row already holds proves the owner. A profile that the caller
already fetched proves the owner when `profile_owner_mismatch()` accepts it.
Otherwise the profile endpoint decides, as the rules below describe. The
`invalid_grant` live recovery runs this check in a worker thread. It refuses a
payload that it cannot prove and logs a warning with no values.

`reconcile_credentials_from_live_store()` imports live tokens into the stamped
row only when it can prove the owner:

- A live access or refresh token that the row already holds proves the owner,
  with no network request.
- Any other live token is identified through the OAuth profile endpoint,
  with the shared owner rule `profile_owner_mismatch()`. The email must match
  the row, and so must the organization uuid and the account uuid when both
  sides record them. The verdict is cached for each token. A failed request
  is retried after 5 minutes, and a 401 or 403 answer after 15 minutes. A
  profile that names the owner, or another owner, is final for the life of
  the token.
- Nothing else is proof. The stamp survives Claude Code's writes, and jacked
  rewrites the `~/.claude.json` identity on every switch, so neither names the
  owner of the tokens.
- An expired token, a failed request, or a call from an event-loop thread
  proves nothing, so reconcile imports nothing. The async callers, and the
  dashboard account list, run reconcile in a worker thread.
- Reconcile does not import a payload that expires before the tokens the row
  already holds. One exception applies: when the row's Claude Code token is
  known dead (the API refused it, or the window keeper recorded
  `needs_reauth` against the current tokens), an owned payload that expires
  sooner replaces it (#179i). This exception acts only on a fresh read. On
  macOS it also acts only when the Keychain itself holds the payload, because
  the credentials file is a mirror that Claude Code does not update.
- Reconcile never imports the row's own primary access token into the `cc`
  columns. That payload is the primary fallback of `build_oauth_data()`.
- While the `cc` breaker records `invalid_grant`, reconcile and the authority
  guard import follow the dead-chain rule of section 2.1. They never import
  the failed token. Reconcile
  adopts a proven new live refresh token and clears the breaker in the same
  compare-and-swap write. After the write lands, it logs `Account N: adopted
  the live Claude Code refresh lineage after invalid_grant (ownership
  proven)`.

Every activation path (a switch, a launch, an OAuth activation) goes through
`activate_account()`. Inside the switch lease, before the engine writes the
live store, it calls `reconcile_outgoing_before_activation()`. This saves the
active account's live tokens to its row, because Claude Code spends a refresh
token when it rotates it. The store is read fresh, not from the 30-second
cache. The step also runs when the target is the active account. The payload
is then built again from the row, so the activation writes the rotation that
it just saved. When the outgoing row keeps changing, or the step fails, the
activation stops with `busy` and code `outgoing_account_busy`, and writes
nothing. The switch route answers that code with `OUTGOING_ACCOUNT_BUSY`
(409). After the preflight, the engine refuses a write that would put a null
refresh token over a live one of the same account (section 7). That refusal
writes nothing.

`cc_rotation_block()` refuses to exchange a `cc` refresh token that a running
session can hold. `live_holder_account_ids()` names those rows. A row is a
holder when the live authority holds its tokens. A row is also a holder when a
tracked session used it in the last 480 minutes (`HOLDER_WINDOW_MINUTES`).
That window is the life of a Claude Code access token: an idle session keeps
its refresh token in memory and spends it when its access token expires.
After the window, a tracked session stays a holder while its recorded process
still runs it (`tracked_holder_sessions()` in `jacked/session_ancestry.py`).
For example, a laptop that slept all night keeps its sessions. The process
must be alive and must have started before the tracker row was written, so a
reused process id does not count. When jacked cannot read the liveness or
the start time, the session stays a holder. A session with no process id
keeps the time rule only. The check probes each process id once and reads
the start times of the live ones in one pass: `/proc` on Linux, the kernel
on Windows, and one `ps` command on macOS. The dashboard process sweeper
uses the same verdict (`session_processes_running()`). It sends a heartbeat
for a running session, closes a session whose process ended or was reused,
and does not change a session with an unknown answer. When
a holder session has no account id, the holders are unknown. Then the
function returns None, `cc_rotation_block()` reports `holders_unknown`, and
jacked refuses every rotation; one INFO line names the cause until the
holders are known again. A live-chain import never adopts a refresh token
that the endpoint condemned for the row (`cc_refresh_failed_token_fp`),
even after a new authorization revived the chain.

One case still loses a refresh lineage. Claude Code rotates the token, and the
access token expires before any reconcile pass sees it (for example, the
computer sleeps). A switch then overwrites the item. Reconcile logs a warning
when it refuses a payload whose refresh token jacked does not hold (#154).

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
9. Remote OAuth may save account data. A remote dashboard may activate local
   credentials only while the persisted remote-access setting is enabled and
   the client is inside the enabled scope; the setting itself is writable from
   loopback only, so reaching the port never grants credential control.
10. Per-account launch directories remain inputs until exact scoped
    consumption is separately certified.

## 13. Primary implementation map

| File | Responsibility |
| --- | --- |
| `jacked/credentials/canonical.py` | Strict JSON parsing, canonical bytes, identity, digest |
| `jacked/credentials/capabilities.py` | Topology-keyed capability registry (platform, config mode, build floor) |
| `jacked/credentials/runtime.py` | Production capability assembly and activation entry points |
| `jacked/credentials/macos_store.py` | Keychain authority adapter driven by the signed security tool |
| `jacked/atomic_io.py` | The one atomic file writer and the Windows-aware replace retry (#237i) |
| `jacked/credentials/file_store.py` | Cross-platform durable JSON file adapter |
| `jacked/credentials/resolver.py` | Authority/mirror consensus and snapshot publication |
| `jacked/credentials/transaction.py` | Switch state machine and outcome axes |
| `jacked/credentials/recovery.py` | Observation-only pending-operation recovery |
| `jacked/web/credential_repository.py` | Secret-free SQLite transaction journal adapter |
| `jacked/api/routes/auth.py` | Local switch API, idempotency, status, active observation |
| `jacked/web/oauth.py` | OAuth storage and conditional local activation |
| `jacked/launch.py` | Per-account launch preparation and current scoped limitation |
| `jacked/api/session_observer.py` | Off-hook authority observation and snapshot refresh |
| `jacked/api/authority_guard.py` | Foreign-write and stamped-rotation adoption, desired-account reassertion |
| `jacked/api/stamped_rotation.py` | Stamped path of the authority guard: adoption and the `oauthAccount` repairs (#154i) |
| `jacked/api/stamped_evidence.py` | The `oauthAccount` verdict and the fingerprint of jacked's own writes |
| `jacked/credentials/refresh_guard.py` | The refresh downgrade refusal (#149i) |
| `jacked/credentials/fingerprint.py` | The fingerprint of a published token pair |
| `jacked/web/refresh_verdict.py` | Token fingerprints and the revoked-token verdicts (#152i) |
| `jacked/api/credential_helpers.py` | Credential payload builder, reconcile, shared ownership rule |
| `jacked/web/auth.py` | Chain-aware refresh flow, token chooser, usage fetch, heal sweep |
| `jacked/data/hooks/session_account_tracker.py` | Snapshot-only session spans without relabelling |
