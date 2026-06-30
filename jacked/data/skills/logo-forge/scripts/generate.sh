#!/usr/bin/env bash
# generate.sh — render or edit a logo via Higgsfield (paid plan; run `higgsfield auth login` once).
#
#   usage: generate.sh <out> <base.png|none> "<prompt>"
#     base = a source image for image-to-image (e.g. preserve a face); "none" = text-to-image.
#
#   env overrides:
#     MODEL=flux_2        (default) image-to-image edits that KEEP the source + make ONE change
#            recraft_v4_1           from-scratch VECTOR marks (can return real .svg)
#            nano_banana_2          from-scratch raster — best for a mascot / character
#            flux_kontext           most conservative; only for adding a clearly-separate object
#     TIER=max  RES=2k    (flux_2 only) quality levers
#     AR=1:1              aspect ratio (use 16:9 for a wide wordmark/lockup)
#
# Captures svg/png/jpg/jpeg/webp result URLs — Recraft can hand back an .svg.
set -euo pipefail
OUT="${1:?usage: generate.sh out <base.png|none> \"prompt\"}"; shift
BASE="${1:?need base.png or none}"; shift
PROMPT="${*:?need a prompt}"
command -v higgsfield >/dev/null || { echo "needs Higgsfield CLI (npm i -g @higgsfield/cli; higgsfield auth login)"; exit 1; }
MODEL="${MODEL:-flux_2}"
ARGS=(generate create "$MODEL" --prompt "$PROMPT" --aspect_ratio "${AR:-1:1}" --wait --wait-timeout 7m)
[ "$MODEL" = flux_2 ] && ARGS+=(--model "${TIER:-max}" --resolution "${RES:-2k}")
[ "$BASE" != none ] && ARGS+=(--image "$BASE")
echo "→ $MODEL ${TIER:+($TIER ${RES:-2k})}  base=$BASE"
url="$(higgsfield "${ARGS[@]}" 2>/dev/null | grep -oE 'https://[^ ]+\.(svg|png|jpg|jpeg|webp)' | tail -1)"
[ -n "$url" ] || { echo "generation failed (transient? re-run; don't fire >2 flux_2-max jobs at once)"; exit 1; }
ext="${url##*.}"; ext="${ext%%\?*}"
curl -s -o "$OUT" "$url"
echo "saved -> $OUT   (source type: $ext)"
