"""Shared tray/menu-bar mark renderer.

The jacked status icon is a flexed-arm silhouette (the "jacked" bicep mark,
matching the dashboard's flexed-biceps favicon). It ships as a grayscale
alpha mask (``jacked/data/icons/tray-mark.png``) and is tinted at render
time with a status color, so the Windows/Linux tray (service state) and the
macOS menu bar (usage color, provider dot, update badge) reuse ONE asset.

Why a mask and not font text: the previous icon drew the letter "J" with a
TrueType font. Font strokes are thin, and a 64px render downscaled to the
16px Windows tray turned into an invisible smudge. A bold silhouette
survives that downscale; a mask needs no font resolution at all (the
long-standing Windows font-fallback bug class disappears).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw

MASK_PATH = Path(__file__).parent.parent / "data" / "icons" / "tray-mark.png"


@lru_cache(maxsize=1)
def _load_mask() -> "Image.Image | None":
    """Load the grayscale silhouette mask, or None when the asset is missing
    or unreadable. Catches broad Exception, not just OSError: Pillow reports
    a malformed or oversized file as ValueError or DecompressionBombError,
    and ANY unreadable mask must land on the fallback mark, never propagate
    into the tray thread."""
    try:
        return Image.open(MASK_PATH).convert("L")
    except Exception:
        return None


def _fallback_mark(size: int) -> "Image.Image":
    """Font-free fallback mark when the mask asset is unreadable: a bold
    rounded square with a knocked-out dot — ugly but visible at 16px, which
    is the one non-negotiable property of a tray icon."""
    img = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(img)
    m = max(1, size // 16)
    d.rounded_rectangle([m, m, size - 1 - m, size - 1 - m], radius=size // 5, fill=255)
    r = size // 5
    d.ellipse(
        [size // 2 - r, size // 2 - r, size // 2 + r, size // 2 + r], fill=0
    )
    return img


def render_mark(fill: tuple[int, int, int, int], size: int = 64) -> "Image.Image":
    """Render the arm silhouette tinted with *fill* on a transparent
    ``size`` x ``size`` canvas."""
    if size < 1:
        raise ValueError(f"icon size must be >= 1, got {size}")
    mask = _load_mask()
    mask = mask.resize((size, size), Image.LANCZOS) if mask else _fallback_mark(size)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    img.paste(Image.new("RGBA", (size, size), fill), (0, 0), mask)
    return img
