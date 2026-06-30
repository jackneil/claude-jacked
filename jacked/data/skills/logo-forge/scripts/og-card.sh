#!/usr/bin/env bash
# og-card.sh — build a 1200x630 social link-preview card (the OG/Twitter image that unfurls
# in iMessage, Slack, X). Mark (square) is circle-cropped on the left; headline + tagline right.
#
#   usage: og-card.sh <mark.png> <out.png> "HEADLINE" "tagline" [bg] [accent] [fg] [fontpath]
#     HEADLINE  one or two words — a 2-word headline stacks onto two lines (fits long names)
#     bg        default #171717   accent (headline)  default #E8C766   fg (tagline) default #9AA0A6
#     fontpath  default: first bold sans found — macOS (Futura/Arial Black/Helvetica),
#               Linux (DejaVu/Liberation/Noto, or whatever fontconfig resolves), or Windows (Arial Bold)
#
# Text is rendered in a REAL font (image models garble letters — never generate a wordmark).
set -euo pipefail
command -v magick >/dev/null || { echo "needs ImageMagick — install 'magick' (brew install imagemagick / apt-get install imagemagick)"; exit 1; }
MARK="${1:?usage: og-card.sh <mark.png> <out.png> \"HEADLINE\" \"tagline\" [bg] [accent] [fg] [font]}"
OUT="${2:?need out.png}"; HEAD="${3:?need headline}"; TAG="${4:-}"
BG="${5:-#171717}"; ACCENT="${6:-#E8C766}"; FG="${7:-#9AA0A6}"; FONT="${8:-}"
if [ -z "$FONT" ]; then
  for f in "/System/Library/Fonts/Supplemental/Futura.ttc" "/System/Library/Fonts/Supplemental/Arial Black.ttf" \
           "/System/Library/Fonts/HelveticaNeue.ttc" "/System/Library/Fonts/Supplemental/Arial Bold.ttf" \
           "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" \
           "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" \
           "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" \
           "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf" \
           "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf" \
           "C:/Windows/Fonts/arialbd.ttf"; do
    [ -f "$f" ] && FONT="$f" && break
  done
fi
# last resort: let fontconfig resolve a bold sans (covers any Linux distro's font layout)
[ -z "$FONT" ] && command -v fc-match >/dev/null && FONT="$(fc-match -f '%{file}' 'sans-serif:bold' 2>/dev/null || true)"
[ -n "$FONT" ] || { echo "no usable font found — install a font (e.g. apt-get install fonts-dejavu) or pass one as arg 8"; exit 1; }
B="$(mktemp -d)"; trap 'rm -rf "$B"' EXIT
# circle-crop the mark
magick "$MARK" -resize 430x430 \( -size 430x430 xc:none -fill white -draw "circle 215,215 215,0" \) \
  -alpha set -compose DstIn -composite "$B/mark.png"
magick -size 1200x630 xc:"$BG" "$B/mark.png" -gravity west -geometry +80+0 -composite "$B/c.png"
H1="$HEAD"; H2=""; case "$HEAD" in *" "*) H1="${HEAD%% *}"; H2="${HEAD#* }";; esac
if [ -n "$H2" ]; then
  magick "$B/c.png" -font "$FONT" -fill "$ACCENT" -pointsize 150 -gravity northwest -annotate +560+150 "$H1" \
    -fill '#FAFAFA' -pointsize 150 -gravity northwest -annotate +560+300 "$H2" "$B/c2.png"
  RULE_Y=455; TAG_Y=470
else
  magick "$B/c.png" -font "$FONT" -fill "$ACCENT" -pointsize 150 -gravity northwest -annotate +560+235 "$H1" "$B/c2.png"
  RULE_Y=410; TAG_Y=425
fi
if [ -n "$TAG" ]; then
  magick "$B/c2.png" -fill "$ACCENT" -draw "rectangle 566,$RULE_Y 760,$((RULE_Y+5))" \
    -font "$FONT" -fill "$FG" -pointsize 38 -gravity northwest -annotate +566+$TAG_Y "$TAG" "$OUT"
else
  cp "$B/c2.png" "$OUT"
fi
echo "og card -> $OUT  (1200x630; QA it, nudge geometry if the headline crowds the edge)"
