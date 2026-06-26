---
description: Use to produce a polished, narrated product demo / walkthrough / how-to video for documentation and education (embed in docs, README, release notes, onboarding). NOT bug evidence — that's /qa-video. Pass the feature/flow to demo, or let it auto-detect.
---

> **Tip:** MCP-based browser tools (Chrome DevTools MCP, Playwright MCP) require no bash approval and work instantly with the jacked gatekeeper. For voiceover + muxing you'll want `ffmpeg`; macOS `say` provides offline narration with zero external accounts.

You are producing a **clean, narrated walkthrough video that teaches a feature** for end-user
documentation — not bug evidence (that's `/qa-video`). The output is something you'd embed in
docs, a README, a release note, or onboarding: staged data, deliberate pacing, captions and/or
voiceover, and a **narration script committed to the repo** so the whole thing regenerates when
the UI changes.

## Step 0: Scope the demo

- **What to demo:** if `$ARGUMENTS` names a feature/flow ("the new export wizard", "login →
  first dashboard"), use it. Otherwise infer from what changed (`git diff`/recent commits) or
  ask the user what they want taught.
- **Audience & goal:** a new user learning this capability. One video = one coherent story
  (don't cram the whole app into one clip; suggest separate clips per feature).
- **Where it lands:** ask or default to `docs/videos/` (the MP4 + captions) plus the narration
  script alongside it. Note the embed target (README, docs site, release note).

## Step 1: Write the narration script — the SOURCE OF TRUTH

Everything is driven by a narration script you write FIRST and commit. It's an ordered list of
**segments**, each with an `id`, the `text` to be spoken/captioned, and a `type`:
- `narrate` — talk over the current screen (no interaction; e.g. an intro or a "notice that…").
- `action` — the browser actually does something (click, type, navigate) while narrating.
- `highlight` — call attention to a specific element (zoom/outline) while narrating.

Save it as `docs/videos/<slug>.narration.json` (committed — diff it like code, regenerate on
release):
```json
[
  { "id": "intro",        "type": "narrate",   "text": "Welcome. This shows how to export a report in three steps." },
  { "id": "open-reports", "type": "action",    "text": "From the dashboard, open the Reports tab.", "do": "click Reports nav" },
  { "id": "pick-range",   "type": "highlight", "text": "Pick a date range — the last 30 days is the default.", "focus": "the date-range picker" },
  { "id": "export",       "type": "action",    "text": "Click Export, choose CSV, and your download starts.", "do": "click Export, choose CSV" },
  { "id": "outro",        "type": "narrate",   "text": "That's it — exports run in the background and email you when ready." }
]
```
Keep each segment to one idea and ~1–2 sentences. Order them as the real flow. This script is
what makes the video reproducible: edit the text or steps, re-run, get a fresh video.

## Step 2: Detect the browser tool + recording path

Detect exactly as `/qa-video` Step 0–1 (read `~/.claude/commands/qa-video.md` if needed):
- **Path P — Playwright MCP native video** (preferred: true motion + chapter markers; needs
  `--caps=devtools`). On Playwright, also start a **trace** (Path T) — it's free alongside.
- **Path F — frame-stitch fallback** for any other tool (Chrome DevTools MCP / agent-browser)
  → assemble frames into MP4 (ffmpeg) or animated GIF (ImageMagick, zero-install).
Announce the chosen path. For a *demo* (vs QA evidence), Playwright native video is strongly
preferred — smoother motion reads better for teaching.

## Step 3: Stage a clean set — this is a demo, not a test

A teaching video must look intentional:
- Log in as the **demo persona** with **representative, clean data** (not empty, not debug
  junk, no real PII — use seed/sample data; if the screen would show real customer data, switch
  to a demo account or sanitize).
- Set a **clean, doc-friendly viewport** — 1280×720 or 1920×1080 (16:9 embeds well).
- **Remove noise:** dismiss cookie banners, close debug overlays/devtools panels, hide any
  "localhost"/staging banners, silence notifications.
- Pre-navigate to the true starting point so segment 1 opens on the intended screen.

## Step 4: Record, timed to the narration

Start recording BEFORE the first navigation (trace/video only capture what happens after start).
Then walk the segments **deliberately** — a teaching pace, slower than a QA run:
1. Start the video (and trace on Playwright). Path F: capture frames per sub-step into
   `docs/videos/<slug>/frame_####.png`.
2. For each segment, in order:
   - `narrate` → hold on the current screen for the segment's beat (≥ the spoken duration; see
     Step 5). No interaction.
   - `action` → perform the interaction with visible, unhurried motion; insert a ~0.8–1.5s
     settle after each click/type so the viewer can follow. Use `browser_snapshot` to locate
     elements by role/label.
   - `highlight` → draw attention to the focus element: scroll it into view and, if supported,
     zoom or outline it (Playwright: `evaluate` a temporary outline/box-shadow on the element,
     or use a known highlight API; otherwise center + a brief pause). Remove the outline after.
3. Stop the video (and trace). Record the saved paths.

## Step 5: Voiceover (optional but recommended) + caption timing

The trick that syncs everything: generate per-segment audio, MEASURE each clip's duration, and
time the browser holds to those durations.

- **Offline default — macOS `say`:** per segment, `say -v Samantha -o seg-<id>.aiff "<text>"`,
  then read its duration (`ffprobe -i seg-<id>.aiff -show_entries format=duration -of csv=p=0`)
  into a `durations.json`. Drive each segment's on-screen hold for at least that long.
- **Higher quality (only if the user has it):** an external TTS (e.g. ElevenLabs) — same
  segment→audio→duration flow. Don't assume an API key; ask, and fall back to `say`.
- **Captions:** when voiceover WAS generated, derive caption timings from `durations.json`
  (the measured per-segment lengths) so the text tracks the audio exactly. Only on the
  no-voiceover path use a fixed reading-speed estimate (~15 chars/sec). Either way, write a
  `.vtt`/`.srt` from the segment text and ship the sidecar or burn it in (Step 6 `subtitles=`).

## Step 6: Assemble the final video

- **Path F frames → video:** `ffmpeg -y -framerate <fps> -pattern_type glob -i 'frame_*.png' -vf "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p" -c:v libx264 -movflags +faststart out.mp4` (or ImageMagick GIF if no ffmpeg).
- **Build one narration track** from the per-segment clips (in `narration.json` order) with a
  short silence pad between segments, then mux it onto the silent recording:
  ```bash
  # 0.4s silence pad, then a concat list: seg-intro.aiff, gap.aiff, seg-open-reports.aiff, …
  ffmpeg -y -f lavfi -t 0.4 -i anullsrc=r=44100:cl=stereo gap.aiff
  ffmpeg -y -f concat -safe 0 -i list.txt -c:a aac -b:a 192k narration.aac   # list.txt = file 'seg-*.aiff' / file 'gap.aiff' lines
  # mux audio onto the silent video (<silent-video> = the .webm from Path P, or out.mp4 from Path F)
  ffmpeg -y -i <silent-video> -i narration.aac -c:v libx264 -c:a aac -b:a 192k -shortest demo.mp4
  ```
  `-shortest` trims to the shorter track so a slight timing drift never leaves frozen frames or
  trailing silence.
- **Captions:** ship `demo.vtt` next to `demo.mp4` (docs players show it), or burn in with
  `-vf "subtitles=demo.vtt"` if the embed target can't show a sidecar.
- **Polish (optional):** a 1–2s title card intro/outro (ffmpeg or a stitched frame). Keep it
  short — teaching, not advertising.

## Step 7: Deliver into the docs

- Write `demo.mp4` (+ `demo.vtt` + the `.narration.json`) to the chosen docs location.
- Provide the **embed snippet** for the target (Markdown image/link, an HTML `<video controls>`
  with the `.vtt` track, or the docs-site shortcode).
- State that the video is **regenerable**: the narration script is committed, so on a UI change
  you re-run `/demo-video` to produce an updated clip — no manual re-recording.

## Step 8: Verify the artifact is real

Don't report success on a file existing — prove it:
- `ffprobe demo.mp4` → duration > 0, a **video stream AND (if voiceover) an audio stream**,
  sane resolution. (`magick identify` for a GIF: frame count > 1.)
- File size is non-trivial; spot-check that the captions/narration text matches what's on screen
  at that timestamp.

If duration is 0, audio is missing when it should be present, or the codec is junk → it failed;
fix and re-run, don't hand over a broken clip.

---

This command **teaches**; for bug/regression evidence use `/qa-video`; for a fast issue list
use `/qa`; for a pixel-and-function audit use `/aesthetic-dogfood-audit`.
