# X API setup + gotchas (read before first use)

## What you need to POST
Posting a tweet requires **OAuth 1.0a user context** — FOUR values, all from the same app:
- API Key (consumer key) — ~25 chars
- API Key Secret (consumer secret) — ~50 chars
- Access Token — starts with `<numericUserId>-...` (~50 chars). The number-dash prefix is how you know you grabbed the right one.
- Access Token Secret — ~45 chars

**The Bearer Token is app-only and CANNOT post.** **OAuth 2.0 Client ID/Secret** (the value under "User authentication settings", decodes to `...:1:ci`) also can't post directly — it needs a browser auth-code flow. Don't confuse these with the four above.

## One-time setup at developer.x.com (console.x.com)
1. Create/choose an app. **User authentication settings → set App permissions to "Read and write"** FIRST (and OAuth 1.0a enabled). Callback/website can be `https://example.com/...` placeholders.
2. **Keys & Tokens** tab:
   - Under **OAuth 1.0 Keys** → **Consumer Key** → it must show, and the access-token row must say **"Read and write"**. If it says "Read", the permission step above wasn't saved → fix it.
   - **Access Token** → **Generate** → copy BOTH the Access Token and Access Token Secret (shown once).
3. Store all four in `~/.secrets/x.env` (chmod 600), exported as env vars:
```bash
export X_API_KEY="..."
export X_API_KEY_SECRET="..."
export X_ACCESS_TOKEN="..."
export X_ACCESS_TOKEN_SECRET="..."
```
Add `[ -f "$HOME/.secrets/x.env" ] && source "$HOME/.secrets/x.env"` to `~/.zshrc` (or `~/.bashrc`) so every shell has them.

## 401 Unauthorized — the gotchas (in order of likelihood)
- **Consumer secret doesn't match the key.** A pasted/stale secret is the #1 cause. Fix: regenerate the API Key & Secret, then re-mint the access token (next point), and store all four fresh.
- **Regenerating the consumer key invalidates the access token.** Access tokens are signed with the consumer secret, so after regenerating the API key/secret you MUST regenerate the access token too — and do it LAST.
- **Wrong value grabbed.** Bearer token / OAuth2 client id can't post (see top). The access token has the `digits-dash` prefix.
- The signer in `scripts/x_post.py` is textbook OAuth 1.0a HMAC-SHA1; if `verify` returns 200 (`users/me` shows the handle), signing is correct and any failure is creds/permissions.

## Pulling a clip out of macOS Photos
**macOS only** — on Linux/Windows, screen-record the demo directly (e.g. with the system recorder or OBS) and skip this section. On macOS, originals are renamed to UUIDs and are often **iCloud-only** (not on disk under "Optimize Mac Storage"). Two routes:
- **AppleScript export (downloads from iCloud automatically):**
```bash
mkdir -p /tmp/clip
osascript -e 'tell application "Photos" to export (every media item whose filename is "IMG_XXXX.MOV") to (POSIX file "/tmp/clip") with using originals'
```
  (First run may trigger a one-time "allow control Photos?" prompt.)
- **Find the on-disk path via the DB** (only works if the original is actually downloaded):
```bash
LIB="$HOME/Pictures/Photos Library.photoslibrary"; cp "$LIB/database/Photos.sqlite" /tmp/ph.sqlite
sqlite3 /tmp/ph.sqlite "SELECT a.ZDIRECTORY||'/'||a.ZFILENAME FROM ZASSET a JOIN ZADDITIONALASSETATTRIBUTES aa ON aa.ZASSET=a.Z_PK WHERE aa.ZORIGINALFILENAME='IMG_XXXX.MOV';"
# original at: $LIB/originals/<that path>   (derivatives/thumbnails are under resources/)
```

## Chunked video upload (what scripts/x_post.py does)
v1.1 `https://upload.twitter.com/1.1/media/upload.json`, OAuth 1.0a:
- **INIT** (POST form: command,total_bytes,media_type=video/mp4,media_category=tweet_video) → media_id
- **APPEND** (POST, query params command/media_id/segment_index + the chunk as multipart `media`; sign only the query params, NOT the binary) — 4MB chunks
- **FINALIZE** (POST form) → may return `processing_info`
- **STATUS** (GET) poll until `state == succeeded`
Then `POST /2/tweets` with `{"text":..., "media":{"media_ids":[id]}}`. Replies: add `{"reply":{"in_reply_to_tweet_id": id}}`.

## Tiers / cost
Free and Pay-Per-Use both allow posting + media upload. Pay-Per-Use bills per call (cents) — fine for a launch. Reads (e.g., `users/me`, fetching the posted tweet) also count.
