"""Tests for jacked.service.icon — the shared tray/menu-bar mark renderer."""

import pytest

pytest.importorskip("PIL")

from PIL import Image  # noqa: E402

from jacked.service import icon  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_mask_cache():
    icon._load_mask.cache_clear()
    yield
    icon._load_mask.cache_clear()


def _coverage(img: "Image.Image", size: int) -> float:
    small = img.convert("RGBA").resize((size, size), Image.LANCZOS)
    data = small.tobytes()
    solid = sum(1 for i in range(0, len(data), 4) if data[i + 3] > 128)
    return solid / (size * size)


def test_mask_asset_ships_and_loads():
    assert icon.MASK_PATH.exists(), "jacked/data/icons/tray-mark.png must ship"
    mask = icon._load_mask()
    assert mask is not None
    # Any square mask >= 64px is a legitimate asset (render_mark resizes);
    # do not pin the current 256px master — that is an implementation detail.
    assert mask.size[0] == mask.size[1] >= 64


def test_render_mark_tints_with_fill():
    img = icon.render_mark((239, 68, 68, 255), 64)
    assert img.size == (64, 64)
    data = img.convert("RGBA").tobytes()
    red = sum(
        1
        for i in range(0, len(data), 4)
        if data[i + 3] > 128 and data[i] > 200 and data[i + 1] < 120
    )
    assert red > 400, "tinted silhouette pixels missing"


def test_render_mark_survives_16px():
    """The whole point of the mark: legible in a 16px Windows tray cell."""
    # Calibration: the shipped arm mark covers ~42% of a 16px cell; the
    # old font-bug speck covered ~2%. A 15% floor separates them cleanly
    # while leaving room for a legitimately lighter future mark.
    cov = _coverage(icon.render_mark((255, 255, 255, 255), 64), 16)
    assert cov > 0.15, f"mark covers only {cov:.0%} at 16px"


def test_missing_asset_falls_back_to_visible_mark(monkeypatch, tmp_path):
    """A broken install (asset missing) must still show SOMETHING in the
    tray — the font-free fallback, never an exception or an empty image."""
    monkeypatch.setattr(icon, "MASK_PATH", tmp_path / "nope.png")
    icon._load_mask.cache_clear()
    img = icon.render_mark((34, 197, 94, 255), 64)
    assert _coverage(img, 16) > 0.15
