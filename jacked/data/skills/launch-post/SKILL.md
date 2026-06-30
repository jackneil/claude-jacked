---
name: launch-post
description: Use when the user wants to announce, launch, promote, ship, or showcase a project, repo, feature, demo, or release on X (Twitter) — including deciding what to highlight, creating the media, and publishing the post + replies. LinkedIn support planned.
---

# launch-post

## Overview

Turn whatever is in the current repo/codebase into a high-performing X (Twitter) post — pick the most showcase-worthy thing, frame it for reach, create the **media**, and publish the tweet + threaded replies via the X API.

**Three non-negotiable principles (learned the hard way):**
1. **Media is mandatory.** Text-only posts get buried. Video > image > nothing. The ranker explicitly rewards video views and dwell.
2. **The link goes in the FIRST REPLY, never the main tweet.** Off-site links are reach-suppressed.
3. **Never post without the human's explicit "go."** It's their public account — outward-facing and irreversible. Show the exact text + the media, get approval, then fire.

This skill is general-purpose (SaaS, dev tools, side projects, personal wins) — not just kid stuff.

## Workflow

1. **Survey the work.** Read `README`, `git log --oneline -20`, the actual product/feature. Find the ONE most surprising, concrete, demoable thing — the "wow." Don't list features; find the hook.
2. **Pick a hook angle** (see Framing). Draft the main tweet (≤280 chars unless the account has X Premium → long-form OK), the link reply, and an optional second reply.
3. **Make the media** (see Media — mandatory). Verify it's real and looks right (eyeball a frame).
4. **Get approval.** Show the user the final text + a media frame. Wait for an explicit "go." Offer wording tweaks.
5. **Publish** with `uv run scripts/x_post.py launch ...` (uploads media, posts main tweet + replies).
6. **Verify live** via API GET, hand over the link, and tell them to **reply to early replies themselves** (that's the biggest reach lever — see weights).

## Framing the post

Find the hook, then pick ONE angle:
- **Concrete story** — a specific, slightly-unbelievable real moment ("My kid built a video game by talking"). Highest reshare potential. Authentic > polished.
- **Contrarian positioning** — "Everyone does X; this does the opposite." Strong for a builder audience.
- **Builder-to-builder** — speak to the exact community that follows the account.
- **Heartfelt / mission** — founder-with-a-why.

Rules: lead with a scroll-stopping first line; **be specific** (real names, numbers, the weird detail) — specifics outperform vague; end on a **reply-bait question** anyone can answer in 5 seconds; do NOT beg for RTs/follows (reads as spam → mute risk).

## X ranking weights → what to do

Real multipliers from the open-sourced algorithm (source: github.com/igorbrigadir/awesome-twitter-algo):

| Signal | Weight | Implication |
|---|---|---|
| Reply the author then engages with | **+75** | Author MUST reply to early replies (first 30–60 min). |
| Reply | **+27** | A reply ≈ 54 likes. Engineer the post to pull replies. |
| Profile click → engage | +12 | A hook that makes people curious about the account helps. |
| Reply/like in-convo, or **dwell ≥2 min** | +11 | Native media + a post worth reading raises dwell. |
| Retweet | +1 | Don't optimize for (or beg for) RTs. |
| Like | +0.5 | Likes barely move it. |
| Mute / block | **−74** | No spam, no ragebait, no follow-for-follow. |
| Report | **−369** | Never deceptive/clickbait-false. |

Also documented: **off-site links are reach-suppressed** (→ link in first reply), and **replies + dwell are prioritized**.

## Media (mandatory)

Pick the most honest, on-message option:
- **Real product footage (best):** screen-record the app/feature/demo running. The user's own clip of the real thing is gold.
- **Generated hero art:** Higgsfield for the art via the bundled **logo-forge** skill's `scripts/generate.sh` (installed alongside this skill — `~/.claude/skills/logo-forge/` on Claude Code, `~/.agents/skills/logo-forge/` on Codex), ImageMagick for crisp text/composition. Use for an image when no live footage fits.
- **Honesty rule:** NEVER post AI-generated or unrelated footage as if it's the real product/result. Eyeball an extracted frame to confirm content before posting.

**X video spec** (transcode anything to this): H.264 High + AAC, `yuv420p`, `+faststart`, ≤512MB, ≤2:20, ≤1080p. Strip junk iPhone data streams with explicit stream mapping:
```bash
ffmpeg -y -i in.mov -map 0:v:0 -map 0:a:0 -c:v libx264 -profile:v high \
  -pix_fmt yuv420p -crf 23 -preset fast -r 30 -c:a aac -b:a 128k \
  -movflags +faststart out.mp4
```
**Pulling a clip from macOS Photos** (macOS only — on Linux/Windows just screen-record the demo directly): originals are often iCloud-only, not on disk. See `reference/x-api-setup.md` (query `Photos.sqlite` for the UUID path, or `osascript` Photos export `with using originals` to force the iCloud download).

## Publishing

One-time credential setup + the gotchas that cause 401s: **`reference/x-api-setup.md`** (read it before first use). Creds live in `~/.secrets/x.env`; `scripts/x_post.py` reads them from the environment.

```bash
source ~/.secrets/x.env
cd <this-skill-dir>/scripts
uv run x_post.py verify                            # confirm auth (prints @handle)
# stage text in files, then:
uv run x_post.py launch video.mp4 main.txt reply1.txt reply2.txt
```
`uv run` reads the script's PEP 723 inline metadata and provisions `requests` automatically — no venv or `pip install` needed (and no bare `python`/`pip`). `launch` uploads the video (chunked), posts the main tweet with it, then threads the replies. The final `DONE` URL is built from the **authed** account's handle (fetched via `users/me`), so it works for any account — nothing is hardcoded. `verify` / `upload <file>` / `tweet`/`reply` exist for partial steps. Stage tweet text in **files** (avoids shell-quoting hell with emoji/newlines).

After posting, verify it's actually live with an API GET (text + `attachments.media_keys` present), then hand over `https://x.com/<handle>/status/<id>`.

## Common mistakes (all hit in practice)

| Mistake | Fix |
|---|---|
| Text-only post | Always attach media (video ideal). |
| Link in the main tweet | Put the link in the FIRST REPLY. |
| Faking the demo footage | Only real, verified media. Eyeball a frame. |
| 401 Unauthorized | Consumer secret must match its key; re-mint all 4 keys together. See reference. |
| Regenerated consumer key, still 401 | Regenerating the consumer key kills the access token — re-mint the access token LAST. |
| Grabbed Bearer Token / OAuth2 client id | Those can't post. Need OAuth 1.0a **access token + secret** (numbers-dash prefix). |
| Posted without approval | Never. Show text + media, get explicit "go." |
| Creds committed | gitignore `.playwright-mcp/` and any temp cred files; creds live only in `~/.secrets/x.env`. |

## Planned

LinkedIn post from the same survey/framing (different API + auth). Not built yet — do X only for now.
