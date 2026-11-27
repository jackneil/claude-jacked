"""Tests for jacked.testdiet.model: the text helpers that every printed line goes through."""

from __future__ import annotations

import sys
import unicodedata

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


def test_one_line_shows_a_lone_surrogate_as_an_escape():
    import io

    name = model.git_name(b"t/test_\xff.py::test_a")
    text = model.one_line(name)
    assert text == "t/test_\\udcff.py::test_a"
    stream = io.TextIOWrapper(io.BytesIO(), "utf-8", errors="strict")
    stream.write(model.clean_text(text))
    stream.flush()


# Every format character (category Cf): zero-width characters, U+FEFF, the Tags block, the bidi set.
FORMAT = [chr(n) for n in range(sys.maxunicode + 1) if unicodedata.category(chr(n)) == "Cf"]


def test_the_format_sweep_holds_the_known_hidden_characters():
    assert {"​", "‍", "﻿", "\U000e0041", "‮", "⁦"} <= set(FORMAT)


@pytest.mark.parametrize("ch", FORMAT, ids=lambda ch: f"U+{ord(ch):04X}")
def test_one_line_escapes_every_format_character(ch):
    text = model.one_line(f"a{ch}b")
    assert ch not in text and text.isascii()
    width = 4 if ord(ch) <= 0xFFFF else 8
    assert text == f"a\\{'u' if width == 4 else 'U'}{ord(ch):0{width}x}b"


@pytest.mark.parametrize("ch", FORMAT, ids=lambda ch: f"U+{ord(ch):04X}")
def test_emit_escapes_every_format_character(ch, capsys):
    model.emit(f"a{ch}b c")
    err = capsys.readouterr().err
    assert ch not in err and err.isascii() and len(err.splitlines()) == 1


# --------------------------------------------------------------------------- git output with a name that is not UTF-8

def _fsdecode_like_windows(monkeypatch):
    """os.fsdecode as Windows has it: UTF-8 with surrogatepass, so the byte 0xff raises."""
    import os

    def strict(value):
        return value if isinstance(value, str) else bytes(value).decode("utf-8", "surrogatepass")

    monkeypatch.setattr(os, "fsdecode", strict)


def test_git_name_decodes_a_name_that_is_not_utf8_the_same_on_every_os(monkeypatch):
    _fsdecode_like_windows(monkeypatch)
    assert model.git_name(b"tests/test_\xff.py") == "tests/test_\udcff.py"
    assert model.git_name("tests/é.py".encode()) == "tests/é.py"
    assert model.git_name(b"tests/test_\xff.py").encode("utf-8", "surrogateescape") == b"tests/test_\xff.py"


def test_git_output_with_a_name_that_is_not_utf8_never_calls_fsdecode(monkeypatch, tmp_path):
    from jacked.testdiet import loss, tree

    _fsdecode_like_windows(monkeypatch)
    assert tree.output_lines(b"tests/test_\xff.py\nb.py\n") == ["tests/test_\udcff.py", "b.py"]
    zero = "0" * 40
    diff = (b":100644 100644 " + b"1" * 40 + b" " + b"2" * 40 + b" M\0tests/test_\xff.py\0"
            b":100644 000000 " + b"3" * 40 + b" " + zero.encode() + b" D\0tests/test_ok.py\0")
    monkeypatch.setattr(tree, "git", lambda *args, **kwargs: (0, diff))
    touched, skipped = loss._touched(tmp_path, "a" * 40, "b" * 40, {})
    assert [path for path, *_ in touched] == ["tests/test_\udcff.py", "tests/test_ok.py"] and skipped == []
    monkeypatch.setattr(tree, "git", lambda *args, **kwargs: (0, b"tests/test_\xff.py\0b.py\0"))
    same = model.TreeId(commit="c" * 40, tree="d" * 40, clean=True)
    assert tree.changed_paths(tmp_path, same, same) == ["b.py", "tests/test_\udcff.py"]
    monkeypatch.setattr(tree, "git", lambda *args, **kwargs: (0, b"sub_\xff/\n"))
    assert tree.work_folder(tmp_path) == "sub_\udcff"


def test_no_testdiet_module_calls_fsdecode_on_git_output():
    import pathlib

    import jacked.testdiet as package

    hits = [f"{path.name}:{n}" for path in sorted(pathlib.Path(package.__file__).parent.glob("*.py"))
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if "os.fsdecode(" in line]
    assert hits == [], "decode git output with model.git_name: " + ", ".join(hits)
