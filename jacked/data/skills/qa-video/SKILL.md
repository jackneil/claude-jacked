---
name: qa-video
description: Use when you want a playable video recording of a QA/test session plus a synced narrated journey — not just screenshots. For regression evidence, "it worked" proof, bug reproduction with motion, or a recorded walkthrough. Pass a URL as argument, or let it auto-detect the running app.
---

First, check if a repo-scoped version exists in the current project:
1. If `.claude/skills/qa-video/SKILL.md` exists (Glob) → read and follow it instead of this file.
2. If `.claude/commands/qa-video.md` exists (Glob) → read and follow it instead (legacy `/jacked-setup` output).
Otherwise follow the engine below.

`/qa-video` is `/qa`'s sibling: it drives the same kind of UI journey but produces a **playable video record** — true-motion via Playwright MCP's native video (chapter markers), or a frame-stitched MP4/GIF for any other browser tool — plus a video-synced narration doc. On Playwright it also emits an interactive **trace.zip** (per-action DOM + console + network, opened at trace.playwright.dev) as the richest QA evidence. Reach for `/qa` when you just need an issue list; reach for this when you need *evidence in motion*.

<!-- ENGINE -->

> **Tip:** MCP-based browser tools (Chrome DevTools MCP, Playwright MCP, Claude-in-Chrome) require no bash approval and work instantly. If using `agent-browser`, pre-approve it once by adding `Bash(npx agent-browser:*)` to your permission allowlist.

You are a QA engineer recording a **video** of a test journey — a playable file plus a video-synced narration — so it can be attached to a bug report or kept as regression evidence. This is `/qa`'s sibling: same kind of UI walk, but the output is *evidence in motion*, not just an issue list.

## Step 0: Decide the recording path (do this FIRST — it determines everything)

There are two ways to get motion into a file — and, when the driver is Playwright MCP, a third, richer evidence artifact (an interactive **trace**) to emit alongside the video. Pick **deterministically by probing what's available**, up front — do not discover mid-session that you have no way to produce a video (the #1 failure mode).

**Path P — Playwright MCP native video (PREFERRED: true motion + chapter markers).**
Playwright MCP records the session to a WebM with chapter markers, but only when the server runs with the `--caps=devtools` capability. Probe it:
- If a `browser_start_video` tool is available (e.g. `mcp__plugin_playwright_playwright__browser_start_video`) → Path P is live. The driver for the journey **must** be Playwright MCP (video records the Playwright-driven browser).
- If Playwright MCP is present but `browser_start_video` is **not** exposed, the server lacks the capability. Enable it once:
  ```bash
  claude mcp add -s user playwright-video -- npx @playwright/mcp@latest --caps=devtools
  ```
  (or add `"--caps=devtools"` to the existing Playwright MCP args). Then tell the user: *"Run that, restart Claude Code so the video tools load, and re-run `/qa-video` for true-motion video with chapters. Continuing now with the frame-stitch fallback."* — and proceed on Path F.

**Path T — Playwright trace (BEST EVIDENCE; emit ALONGSIDE Path P whenever the driver is Playwright MCP).**
A Playwright `trace.zip` is the QA-grade artifact: per-action Before/Action/After DOM snapshots, a screencast filmstrip, and console + network logs that **filter per action**, plus an Errors timeline — i.e. the exact "console/network at that moment" signal this skill otherwise reconstructs by hand in the doc (Step 5). The same `--caps=devtools` capability that exposes the video also exposes the trace tools, so on Playwright you get it nearly for free:
- If a `browser_start_tracing` tool is available (e.g. `mcp__plugin_playwright_playwright__browser_start_tracing`) → Path T is live.
- **Priority on Playwright: emit BOTH.** The native video is the human-watchable, drag-into-Slack clip; the `trace.zip` is the interactive, per-action evidence a reviewer steps through. Treat the trace as the primary regression/bug artifact and the video as the shareable companion.
- If `--caps=devtools` isn't enabled, the single `claude mcp add … --caps=devtools` above turns on video **and** trace together — they ship in the same capability.

**Path F — frame-stitch (FALLBACK: works with ANY browser tool — Chrome DevTools MCP, agent-browser, Claude-in-Chrome).**
Capture screenshots at every sub-step, then assemble them into a video. Probe the encoder **once, up front**:
- `magick`/`convert` present (ImageMagick — usually already installed)? → **animated GIF, zero install.** This is the default fallback.
- `ffmpeg` present? → **MP4** (smaller, smoother, GitHub/Slack-native). Prefer over GIF when available.
- Neither? → install the repo-sanctioned way: `uv pip install imageio imageio-ffmpeg` (ships a static ffmpeg — no brew/sudo) → MP4. If fully offline with no ImageMagick, say so plainly rather than fake it.

**Do NOT** default to macOS `screencapture -v` — it trips the Screen-Recording TCC permission prompt an agent can't click, and fails silently on headless browsers.

**Announce the chosen path** ("Recording via Playwright MCP video with chapters + interactive trace" / "Recording via Playwright MCP video with chapters" / "Frame-stitch → MP4 via ffmpeg" / "Frame-stitch → animated GIF via ImageMagick") before you start.

## Step 1: Detect the browser driver

Detect the driver exactly as `/qa` Step 1: **Chrome DevTools MCP** (preferred) → **Playwright MCP** → **Claude-in-Chrome** → **agent-browser CLI**. Read `~/.claude/skills/qa/SKILL.md` Step 1 if you need the exact probe calls and the setup hints to print when none are found.

Caveat from Step 0: **if you chose Path P/T, the driver is Playwright MCP** (video, trace, and driving are all the same server). On Path F, any detected driver works.

## Step 2: Scope the journey (URL + what to walk + credentials)

- **Isolate → PROVE it → only THEN record freely. Fail closed.** This drives and clicks through the UI and may write (submit/save/delete) — especially when reproducing a bug. **First get an isolated copy** (best available): (1) a **PR / preview / ephemeral deploy** (check `gh pr checks` / the PR's deployment links); (2) **spin it up locally** — dev server + a local DB with seed/fixture data (`docker compose up`, `manage.py runserver`, `npm run dev`/`pnpm dev`, a `seed`/`migrate` command, `.env.local`); (3) a disposable staging. **Then, BEFORE the first write, you are READ-ONLY until you affirmatively confirm ALL of:** (a) **host** is `localhost`/`127.0.0.1`/the EXACT preview URL — never the prod domain; (b) **DB** — the running PROCESS is on a local/throwaway DB, read from the live process (`ps eww <pid>`, `/proc/<pid>/environ`) or an app endpoint, NOT a dotfile (a preview/remote URL alone does NOT prove the DB — if you can't read its env, stay read-only); (c) **outbound side-effects** — email/payment/webhook integrations are sandboxed or disabled (a local DB won't stop a real charge or email); (d) **you started it** — a server you merely found listening isn't proof. ANY doubt → it's production, record READ-ONLY (navigate + observe only). **Only once ALL pass: reproduce destructive bugs freely** — nothing hits real data or users. **This gate governs EVERY write — including logging in** (auth itself can create a session or fire a real notification), form submits, and any re-run: on an unproven/production target, navigate + observe only, do NOT log in. A URL passed in clears only the Host check, never the rest.
- **URL:** if `$ARGUMENTS` contains a URL, use it as the target — still subject to the full gate above before any write OR login. Otherwise detect a running dev server (conversation context, then `lsof -i -P -sTCP:LISTEN | grep -E ':(3000|3001|4200|5000|5173|5174|8000|8080|8765|8888) '`; a found-but-not-started server fails check (d)). If none, ask.
- **What to walk:** if the user named a flow ("login → dashboard"), follow it. Otherwise scope to what changed (`git diff --name-only HEAD`, UI files) like `/qa` Step 2 — record the journey through the affected areas.
- **Credentials:** if auth is needed, find creds in `.env*` exactly as `/qa` Step 5 (announce variable names only, never values; skip `DB_`/`AWS_`/infra vars). **Never fake a login** — if you can't get past auth, record up to the login wall and narrate that the rest is blocked on credentials.
- Confirm the app is actually up before recording: `curl -sS -o /dev/null -w '%{http_code}' <url>` — a blank/error page is only worth recording if that *is* the bug.

## Step 3: Record the journey

Walk the journey as a sequence of **legs** (e.g. "Load login", "Fill + submit", "Dashboard renders"). The critical trick either way: **inject deliberate wait beats** (`browser_wait_for` / equivalent) — MCP actions are near-instant, so without ~0.8–1.5s settle pauses after each meaningful action the video is an unreadable blur.

**Start recording BEFORE the first navigation.** The video — and especially the trace/context API — only captures what happens *after* it starts. Begin it after the page has already loaded and you've silently truncated the opening state of the journey (the classic "started the screen recording after the bug already happened" mistake). So: start tracing/video first, *then* navigate.

**Path P/T (Playwright native video + trace):**
1. **Before navigating** (ordering matters — see above): start whatever is live. `browser_start_tracing` (Path T) and/or `browser_start_video { filename: "qa-<area>.webm", width: 1440, height: 900 }` (Path P). On Playwright, start **both** up front so the first-load state lands in both artifacts.
2. Before each leg: `browser_video_chapter { title: "<leg>", description: "<what should happen>" }` — **chapters are your narration spine**, one per leg.
3. Drive the leg with wait beats between actions. Use `browser_snapshot` (accessibility tree) to locate elements by role/label — don't guess selectors.
4. Per leg, also capture evidence for the doc: `browser_console_messages` (JS errors), a `browser_take_screenshot` thumbnail, and note any failed requests. (The trace already records all of this per-action — these stills are the doc's offline backup.)
5. `browser_stop_video` **and** `browser_stop_tracing` → record the saved `.webm` and `trace.zip` paths they print.

**Path F (frame-stitch):**
1. Output dir: `REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)`; `rm -rf "$REPO_ROOT/tmp/qa_video"; mkdir -p "$REPO_ROOT/tmp/qa_video"`. Add `tmp/` to `.gitignore` if absent.
2. Set viewport 1440×900.
3. Screenshot **every meaningful sub-state** — before click, after click, on focus, on input, on validation error, on load — to zero-padded sequential frames: `tmp/qa_video/frame_0001.png`, `frame_0002.png`, … **Over-capture**; more frames = smoother playback. Inject a wait between captures so distinct states land.
4. Per leg, capture console/network + a snapshot for the doc.

## Step 4: Assemble & make it shareable

**Path F — stitch frames** (`DIR="$REPO_ROOT/tmp/qa_video"`):
```bash
# ImageMagick GIF — zero-install default (-delay is in 1/100s; 70 = 0.7s/frame)
magick -delay 70 -loop 0 "$DIR"/frame_*.png "$DIR/qa_session.gif"

# ffmpeg MP4 — preferred when present (GitHub/Slack drag-drop, smaller)
ffmpeg -y -framerate 1.5 -pattern_type glob -i "$DIR/frame_*.png" \
  -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p" -c:v libx264 -movflags +faststart "$DIR/qa_session.mp4"
```
**Path P — convert for sharing:** GitHub rejects some `.webm`. If `ffmpeg` is present, also emit an MP4: `ffmpeg -y -i <session>.webm -c:v libx264 -movflags +faststart -vf format=yuv420p qa_session.mp4`. If not, keep the `.webm` — it plays in browsers and most players.

**Path T — no assembly needed:** the `trace.zip` is already a complete, self-contained artifact — don't stitch or convert it. See Step 5/7 for how a reviewer opens it.

## Step 5: Write the synced narrated journey doc

Per the jacked artifact rule (see `qa.md` / `~/.claude/jacked-reference.md` § Artifact Format), write this **as HTML, not Markdown**. Copy `~/.claude/jacked-templates/plan-template.html` and save to `docs/superpowers/qa/{YYYY-MM-DD}-qa-video-journey.html` (or to the scratchpad if the user wants nothing in the repo).

The doc is **synced to the video** — each leg is a section with:
- the chapter title (Path P) or a `T+M:SS` timestamp (Path F) that matches the video timeline,
- the step description and **expected vs. observed**,
- the **console / network status at that moment** (the real QA signal — JS errors, failed requests),
- the embedded leg screenshot.

Header: app URL, date, browser + viewport, which path produced the video, a PASS/FAIL summary, and a console-errors / failed-requests table. **On Path T, link the `trace.zip` in the header too** — its absolute path plus a one-line "open it at https://trace.playwright.dev" note (see below), right next to the video path.

**Trace as the interactive companion (Path T):** the `trace.zip` *is* the live, per-action view of every "console / network at that moment" row above. A reviewer opens it at **https://trace.playwright.dev** — drag-and-drop the file in, or append `?trace=<accessible-url>` for a hosted trace — then filters the timeline to the failing action to see its Before/Action/After DOM, console, and network in isolation. The viewer loads **entirely client-side and transmits no data externally**, so it's safe to attach even for sensitive apps. Link it from the header so the reader can jump straight from the prose to the interactive evidence.

"Narrated" here means a **written, video-synced journey**. If the user explicitly asks for an **audio voiceover**, generate a line per leg with macOS `say` → AIFF and mux it onto the video with ffmpeg — only when asked (extra dependency, macOS-only).

## Step 6: Verify the artifacts are real (do not skip)

Never report success on a file merely existing — **prove the video plays**:
- `ffprobe <video>` (or `magick identify <gif>`): duration > 0, sane codec/resolution, **frame count > 1**.
- File size is non-trivial (a few KB = a failed/black capture).
- **Trace (Path T):** confirm the `trace.zip` is a real, non-empty archive — `unzip -l <trace.zip>` should list a `trace.trace` entry plus resource files (a bare stub or a few KB means tracing never captured the journey). Don't hand over an empty trace as evidence.

If duration is 0, there's a single frame, or the codec is junk → the capture **failed**. Fix it and re-record; do **not** hand over a broken file as if it worked.

## Step 7: Deliver

Report **absolute paths** to the video, the journey doc, **and the `trace.zip` if Path T ran**. Note that the `.mp4` (and `.gif`) drag-and-drop straight into GitHub issues and Slack; the `trace.zip` is opened interactively at **https://trace.playwright.dev** (drag-and-drop it in, or share a hosted trace via `?trace=<url>`) — it loads fully in the browser and uploads nothing, so it's safe to attach to a bug report. Offer to drop them into the repo (e.g. `docs/qa/`) — but **don't commit anything unless asked**.

**Optional — turn the journey into a durable test.** If the journey passed and is worth keeping green, offer: *"I can emit a `@playwright/test` spec that reproduces this exact journey, so it runs as a regression test."* This is the manual-exploration → automated-test hand-off — keep it an offer, not a default; this command's job is evidence, not test-authoring.

## Step 8: Cleanup

Remove the frame scratch (Path F): `rm -f "$REPO_ROOT/tmp/qa_video"/frame_*.png` — keep the final video + doc. If you created `tmp/qa_video` and it's now empty, remove it.

---

This command **produces a record**; it is not a substitute for issue-finding. For a fast issue list use `/qa`; for a pixel-level polish pass use `/aesthetic-dogfood-audit`. Use `/qa-video` when you need evidence in motion.
