# Design: Claude Code Settings Management

## Problem

`~/.claude.json` is Claude Code's global config — theme, permissions, project data, feature flags. When running per-account sessions via `CLAUDE_CONFIG_DIR`, each account gets its own `.claude.json`. Currently we seed it from the global config, but there's no way to:

- Manage what each account inherits vs. overrides
- Edit Claude Code preferences from the jacked dashboard
- Understand which accounts have diverged from global settings

## Current Behavior (v0.7.4)

On launch, `_seed_claude_config()` copies only safe UX/onboarding keys from `~/.claude.json` into the per-account dir. This runs once — when `hasCompletedOnboarding` is missing. After that, Claude Code owns the per-account `.claude.json` and can modify it freely.

Safe keys copied (`_SAFE_CONFIG_KEYS`):
- `autoUpdates`, `autoUpdatesProtectedForNative`, `showSpinnerTree`
- `claudeInChromeDefaultEnabled`, `penguinModeOrgEnabled`
- `hasCompletedOnboarding`, `lastOnboardingVersion`, `hasSeenTasksHint`
- `hasCompletedClaudeInChromeOnboarding`, `effortCalloutDismissed`
- `opusProMigrationComplete`, `sonnet1m45MigrationComplete`
- `officialMarketplaceAutoInstallAttempted`, `officialMarketplaceAutoInstalled`
- `lastReleaseNotesSeen`, `installMethod`

Keys explicitly excluded (never copied cross-account):
- `userID`, `anonymousId` — analytics identity
- `oauthAccount` — account-specific OAuth data
- `projects` — per-project tool permissions
- All internal caches and timestamps

## Settings Inheritance Model

### Three tiers

1. **Global settings** (`~/.claude.json`) — updated by any normal `claude` session
2. **Account-level overrides** — per-account settings stored in jacked's DB
3. **Effective config** — computed at launch: global safe keys + account overrides merged

### Per-account mode

New `settings_mode` column in `accounts` table:

- `"follow_global"` (default) — merge safe global keys, then apply account-level overrides
- `"diverged"` — use the per-account `.claude.json` as-is, skip global merge

## Editable Settings Allowlist

Only these keys are editable from the jacked UX, with strict type validation:

| Key | Type | Description |
|-----|------|-------------|
| `autoUpdates` | `bool` | Auto-update Claude Code |
| `showSpinnerTree` | `bool` | Show task tree spinner |
| `claudeInChromeDefaultEnabled` | `bool` | Chrome extension integration |
| `penguinModeOrgEnabled` | `bool` | Team/org mode |

All other keys are passed through unchanged during merge — never stripped, never rejected. Unknown keys from Claude Code updates are preserved silently.

## Merge Semantics

**Shallow top-level key replacement.** Each override replaces the entire value at that key:

- Override `autoUpdates` → only that key changes
- Deep merge is a future enhancement if needed
- Unknown keys from Claude Code releases are preserved

## API Endpoints

```
GET  /api/claude-config                                 — read global .claude.json (safe keys only)
PUT  /api/claude-config/{key}                           — update a global .claude.json key
                                                          validates {key} against EDITABLE_KEYS
                                                          validates value type per key
                                                          rejects unknown keys with 400

GET  /api/claude-config/accounts/{id}                   — read per-account effective config
PUT  /api/claude-config/accounts/{id}/mode              — set follow_global or diverged

POST /api/claude-config/accounts/{id}/reset-to-global   — copy global → account (safe, always allowed)
POST /api/claude-config/accounts/{id}/promote-to-global — copy account → global (dangerous)
                                                          requires {confirm: true} in body
                                                          returns diff preview if confirm absent
```

Security:
- `PUT /api/claude-config/{key}` validates against `EDITABLE_KEYS` dict with per-key type enforcement
- `promote-to-global` is separate from `reset-to-global` — different risk levels
- `promote-to-global` requires explicit confirmation, returns diff preview first
- `promote-to-global` only copies `_SAFE_CONFIG_KEYS` from account → global

## Schema Change

```sql
ALTER TABLE accounts ADD COLUMN settings_mode TEXT DEFAULT 'follow_global'
    CHECK(settings_mode IN ('follow_global', 'diverged'));
```

## Launch Flow (updated)

```
1. Read account from DB (includes settings_mode)
2. If "follow_global":
   a. Read global ~/.claude.json, parse JSON
   b. Read existing per-account .claude.json (if any)
   c. Merge safe keys from global into per-account
   d. Read account-level overrides from DB
   e. Apply overrides on top
   f. Write merged config to per-account .claude.json
3. If "diverged":
   a. Leave per-account .claude.json untouched
4. Write .credentials.json (existing logic)
5. Write Keychain (existing logic)
6. exec claude
```

## Schema Drift Strategy

Claude Code adds keys with every release. Strategy:

- **Unknown keys passed through unchanged** during merge — never stripped
- **Diff view shows divergence for known keys only**; unknown keys in collapsed "Other" section
- **No schema version check** — we don't block on unknown keys, just ignore them in the UX
- **Content hashing** for freshness: SHA-256 of `json.dumps(safe_keys, sort_keys=True)` stored in DB alongside `settings_mode`

## UX Design

### New "Claude Config" tab in Settings page

- View/edit global settings (4 editable keys via toggles)
- Per-account settings mode toggle with confirmation dialog
- "Reset to global" button per account
- "Promote to global" button per account (with diff preview + confirmation)
- Diff view showing which safe keys diverge from global
- Read-only view of current `oauthAccount` per account

### Account cards (accounts page)

- Badge: "Global" or "Custom" settings mode
- Quick toggle with confirmation when switching `diverged → follow_global`

### Mode Transition UX

- `follow_global → diverged`: Instant, no data loss. Per-account `.claude.json` starts accumulating local changes.
- `diverged → follow_global`: **Confirmation required.** "This will overwrite your custom settings with global settings on next launch. Continue?" No backup — user can "promote to global" first.

## Future Considerations

- Claude Code may fix Keychain namespacing (issue #20553)
- If Claude Code adds `CLAUDE_CONFIG_DIR`-aware settings, align with their approach
- Deep merge for `projects` key (per-repo tool permissions per account)
- Per-project settings are the most complex tier
