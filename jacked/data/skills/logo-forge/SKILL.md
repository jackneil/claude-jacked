---
name: logo-forge
description: Use when designing or upgrading a brand logo, favicon/app icon, or social link-preview cards (Open Graph / Twitter cards that unfurl in iMessage, Slack, X/Twitter) for a repo or product. Triggers on "design a logo", "make me a logo", "better logo / favicon", "brand mark", "app icon", "social card", "og image", "link preview", "iMessage preview", "twitter card", or being pointed at a repo that needs branding. Generates real logos via Higgsfield image models, builds the full favicon + social-card set with ImageMagick, and deploys them into the target repo (Next.js or static; branch+PR; worktree-safe alongside a live session).
---

# logo-forge

Design a real, ownable logo set for any repo/product and ship it: **icon + favicon set + apple-touch + Open Graph / Twitter social cards**, deployed into the target repo.

Scripts live in `scripts/` next to this file — invoke them by their path inside this skill's directory. When installed by claude-jacked that's:
`~/.claude/skills/logo-forge/scripts/{generate,make-favicons,make-transparent,og-card}.sh` (Claude Code) or `~/.agents/skills/logo-forge/scripts/…` (Codex).

## Prereqs
- **Higgsfield CLI** (paid, preferred image engine): `higgsfield auth login` once. `higgsfield account status` = credits. This is the image generator — NOT ImageMagick.
- **ImageMagick** (`magick`): deterministic composition, favicon export, OG cards. Note: it often has **zero fonts registered** — pass a font file by absolute path (`-font /System/Library/Fonts/Supplemental/Futura.ttc`) and draw shapes as vector `path`/`roundrectangle`, not font glyphs.

## Pipeline

1. **Understand the product.** Read the target repo's `README`, any `brand.*`/theme/CSS, and find where icons live + how they're referenced (`grep -rin 'favicon|apple-touch|og:image|opengraph|icon' app`). Note: what it does, current brand color/tone, framework, and whether a logo already exists (improve on it, don't ignore it).

2. **Find the ONE idea that encodes the product's value.** A great mark's single move *encodes the product* (a shield = guardian; an open book = docs; a donkey hauling saddlebags of EOBs = payer-paperwork grunt-work; a hand snatching a drop = an intercept alert). Not a random sparkle. If a real mascot/face already exists, **preserve it and make ONE change**; if from-scratch, invent freely.
   - For non-trivial brands, fan out a **concept panel** (Workflow): 4-6 lenses generate divergent directions → a judge ranks for memorability, encodes-the-product, 16px legibility, tone, and a *mix of kinds* (icon / monogram / mascot / lockup), assigning a render model to each. Present the shortlist; let the user pick before burning render credits — **they react to pixels, so render the picks and show them, don't make them choose from text alone.**

3. **Render via Higgsfield** (`scripts/generate.sh out base|none "prompt"`, env `MODEL=...`):

   | MODEL | Use for | Reality |
   |---|---|---|
   | `flux_2` (`TIER=max RES=2k`) | image-to-image edits that KEEP a source face/mark + make ONE change; clean graphic marks | The workhorse. Preserves a face well; the only one that does subtle edits. |
   | `recraft_v4_1` | from-scratch **vector** icon / monogram / wordmark | Can return real `.svg`. Premium clean marks. (Often returns a raster PNG instead — capture both.) |
   | `nano_banana_2` | from-scratch **mascot** / rich character | Best character renderer. Keep it geometric or it drifts cute. |
   | `flux_kontext` | adding a clearly-separate object only | Too timid for subtle edits. |

   - **Sequential renders.** Never fire more than ~2 `flux_2`-max jobs at once (they transiently fail) — run a background driver one job at a time with one retry.
   - **Never let a model render TEXT.** Image models garble letters. A wordmark/initials must be set in a **real font** (`og-card.sh` / `magick -font`), or the mark loses the letters. Generate the symbol; typeset the words.

4. **Curate at 16px — always.** Build a strip: full | 32px | 16px (`-filter point` upscale so the favicon reality is visible). One bold element survives 16px; detail dies. So: a detailed mascot/face = the **hero / OG card**; a simple bold mark (monogram, single symbol) = the **favicon**. `open` the candidates in Preview for the user — they judge full-size, not chat thumbnails.

5. **Build the asset set** (ImageMagick, deterministic):
   - Favicon set: `scripts/make-favicons.sh <master.png> <outdir> [flatten-bg]` → `favicon.svg/.ico/16/32/128 + apple-touch-180 + android-192/512`. (`make-transparent.sh` first if you want the bg knocked out — flood-fills white from corners, keeps enclosed white.)
   - Social card: `scripts/og-card.sh <mark.png> <out.png> "HEADLINE" "tagline" [bg accent fg font]` → 1200×630 OG/Twitter image. This is the iMessage/Slack/X unfurl — usually the most important deliverable when someone says "previews".

6. **Deploy into the target repo** (use the SAME filenames it already references → no template edits):
   - **Next.js app-router**: drop files into `app/` by convention — `icon.svg`, `apple-icon.png`, `opengraph-image.png` (+ `opengraph-image.alt.txt`), `twitter-image.png` (+ `.alt.txt`). In `app/layout.tsx` add `metadataBase: new URL(process.env.NEXT_PUBLIC_SITE_URL ?? 'https://<host>')`, an `openGraph` + `twitter: { card: 'summary_large_image' }` block (drive titles from the existing product-name constant — keep white-label intact), and `export const viewport: Viewport = { themeColor: '#…' }`.
   - **Static / FastAPI-Jinja / other**: overwrite the icon file at its existing path (e.g. `app/web/static/favicon.svg`); add `<meta property="og:image">` / `twitter:image` + `<link rel="apple-touch-icon">` in the base template; serve a 1200×630 OG image from static. Reuse existing filenames so no markup changes are needed.
   - A favicon SVG can embed a raster (`<svg><image href="data:image/png;base64,…"/></svg>`) when the source is raster — works everywhere, just heavier; offer a hand-authored bold vector if the tab looks soft at 16px.

## Hard-won rules (don't relearn these)
- **Friendly/helpful, never hostile.** A user-facing mark must not insult or menace the user — killed "RTFM" (a middle finger to non-devs) and a pointing-hand that rendered as an obscene gesture. Cheeky is fine; hostile/criminal/sketchy is not.
- **Face/subject is the hero; any added object stays small.** "Book too big" was a real rejection — at 16px only the hero survives, so size cues as modest accents.
- **A wordmark is set in real type, never generated.** (See above — models garble text.)
- **Show rendered pixels and `open` them; iterate on the user's reaction.** Don't defend a concept from text. They reversed picks once they saw them small.
- **Higgsfield is the preferred (paid) engine.** Pure ImageMagick "marks" look cheap — use IM for composition/export/cards, Higgsfield for the art.

## Deploy safety (shipping into a real repo)
- **Live/shipped repo → branch + PR, never push to main** (unless the user explicitly says use the active branch).
- **A concurrent agent session may be working in the repo.** Do NOT switch the shared working dir's branch (it moves their HEAD and can wipe their uncommitted work). Instead use an **isolated git worktree**: `git -C <repo> worktree add /tmp/<wt> -b <branch> main`, do all edits there, commit only your files (pathspec `git commit -m … -- <files>`, never `-a`), push, open the PR with `gh`, then `git worktree remove`. Verify with `git -C <repo> status` that you touched only your files and left their WIP alone.
- **Commit it.** An uncommitted working-tree file gets silently wiped when another session resets/switches the tree. A branch+PR (or at least a pathspec commit) persists.
- White-label product name lives in env (e.g. `APP_PRODUCT_NAME`) — set that for the deployment rather than baking a brand into a neutral default; the OG image carries the brand visually regardless. Flag the env var + the `metadataBase` host for the user to confirm.

## Typical run
1. Read the repo; find the icon path + product story.
2. (optional) Concept panel → present shortlist → user picks.
3. `generate.sh` the picks sequentially (right model each); curate at 16px; `open` in Preview.
4. `make-favicons.sh` + `og-card.sh` → full set.
5. Deploy via worktree + branch + PR (or the user's chosen branch); commit only your files; report the env var + host to confirm.
