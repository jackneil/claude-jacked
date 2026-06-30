#!/usr/bin/env bash
# make-transparent.sh — clear a white background from line art, KEEP enclosed white.
#
#   usage: make-transparent.sh <in.png> <out.png> [fuzz%]   (fuzz default 12)
#
# Flood-fills white inward from the 4 corners, so the connected background goes
# transparent but the white face fill (enclosed by black outline) stays opaque.
# This is why the donkey/shield faces keep a solid face on a transparent tab.
set -euo pipefail
command -v magick >/dev/null || { echo "needs ImageMagick — install 'magick' (brew install imagemagick / apt-get install imagemagick)"; exit 1; }
IN="${1:?usage: make-transparent.sh <in.png> <out.png> [fuzz%]}"
OUT="${2:?need out.png}"
FUZZ="${3:-12}"
W="$(magick identify -format '%w' "$IN")"
H="$(magick identify -format '%h' "$IN")"
magick "$IN" -alpha set -fuzz "${FUZZ}%" -fill none \
  -draw "alpha 0,0 floodfill" \
  -draw "alpha $((W-1)),0 floodfill" \
  -draw "alpha 0,$((H-1)) floodfill" \
  -draw "alpha $((W-1)),$((H-1)) floodfill" \
  "$OUT"
echo "transparent -> $OUT"
