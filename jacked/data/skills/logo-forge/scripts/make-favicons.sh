#!/usr/bin/env bash
# make-favicons.sh — turn ONE square logo master into a full favicon set.
#
#   usage: make-favicons.sh <master.png> <outdir> [flatten-bg]
#     flatten-bg : background for the OPAQUE apple-touch / android icons.
#                  default "white". Use "none" to keep them transparent,
#                  or a color like "#22B8CF" for a brand-tile icon.
#
# Produces (the conventional filenames most web repos already reference):
#   favicon.svg  favicon.ico  favicon-16x16.png  favicon-32x32.png
#   favicon-128.png  apple-touch-icon.png  android-chrome-192x192.png  android-chrome-512x512.png
#
# Then drop the set into the product's static icons dir (same names = no template edits)
# and hard-refresh (browsers cache favicons hard).
set -euo pipefail
SRC="${1:?usage: make-favicons.sh <master.png> <outdir> [flatten-bg]}"
OUT="${2:?need an output dir}"
FBG="${3:-white}"
command -v magick >/dev/null || { echo "needs ImageMagick — install 'magick' (brew install imagemagick / apt-get install imagemagick)"; exit 1; }
mkdir -p "$OUT"; B="$(mktemp -d)"; trap 'rm -rf "$B"' EXIT

# normalize: trim to the mark, ~10% even padding, square 512, keep transparency
magick "$SRC" -trim +repage -resize 460x460 -background none -gravity center -extent 512x512 "$B/m.png"

# browser favicons (keep alpha)
magick "$B/m.png" -resize 16x16  "$OUT/favicon-16x16.png"
magick "$B/m.png" -resize 32x32  "$OUT/favicon-32x32.png"
magick "$B/m.png" -resize 128x128 "$OUT/favicon-128.png"
magick "$B/m.png" -define icon:auto-resize=16,32,48 "$OUT/favicon.ico"

# apple-touch + android-chrome (OS-masked → want opaque unless you pass "none")
flat(){ # <px> <outfile>
  if [ "$FBG" = none ]; then magick "$B/m.png" -resize "${1}x${1}" "$2"
  else magick "$B/m.png" -background "$FBG" -alpha remove -alpha off -resize "${1}x${1}" "$2"; fi
}
flat 180 "$OUT/apple-touch-icon.png"
flat 192 "$OUT/android-chrome-192x192.png"
flat 512 "$OUT/android-chrome-512x512.png"

# svg = the 512 master embedded (crisp, tiny, drop-in)
b64="$(base64 -i "$B/m.png" | tr -d '\n')"
printf '%s' '<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 512 512" width="512" height="512">
<image width="512" height="512" xlink:href="data:image/png;base64,'"$b64"'"/>
</svg>' > "$OUT/favicon.svg"

echo "favicon set -> $OUT"
ls -1 "$OUT"
