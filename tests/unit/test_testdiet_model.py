"""Tests for jacked.testdiet.model: the text helpers that every printed line goes through."""

from __future__ import annotations

import sys

import pytest

from jacked.testdiet import model

# Every character that str.splitlines() reads as a line boundary.
BOUNDARIES = [chr(n) for n in range(sys.maxunicode + 1) if len(f"a{chr(n)}b".splitlines()) > 1]
BIDI = ["\u061c", "\u200e", "\u200f", *map(chr, range(0x202A, 0x202F)), *map(chr, range(0x2066, 0x206A))]


def test_the_boundary_sweep_finds_the_known_set():
    assert set(BOUNDARIES) == set("\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029")


@pytest.mark.parametrize("ch", BOUNDARIES, ids=lambda ch: f"U+{ord(ch):04X}")
def test_one_line_escapes_every_splitlines_boundary(ch):
    text = model.one_line(f"a{ch}b")
    assert len(text.splitlines()) == 1 and ch not in text
    assert text.startswith("a\\") and text.endswith("b")


@pytest.mark.parametrize("ch", BIDI, ids=lambda ch: f"U+{ord(ch):04X}")
def test_one_line_escapes_every_bidi_control(ch):
    text = model.one_line(f"a{ch}b")
    assert ch not in text and text == f"a\\u{ord(ch):04x}b"


def test_one_line_keeps_the_known_escapes_of_a_line_break():
    assert model.one_line("a\nb\rc") == "a\\nb\\rc"
    assert model.one_line("a\u2028b") == "a\\u2028b"


@pytest.mark.parametrize("ch", BOUNDARIES + BIDI, ids=lambda ch: f"U+{ord(ch):04X}")
def test_emit_prints_one_line_for_a_message_with_a_boundary(ch, capsys):
    model.emit(f"a{ch}b c")
    err = capsys.readouterr().err
    assert err.endswith("\n") and len(err.splitlines()) == 1 and ch not in err[:-1]
    assert err.startswith(model.PREFIX)


def test_emit_never_prints_two_lines(capsys):
    model.emit("a\nb c")
    err = capsys.readouterr().err
    assert err == model.PREFIX + "a\\nb c\n"
