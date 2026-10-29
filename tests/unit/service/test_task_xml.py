"""Task Scheduler XML encoding (#165i).

schtasks /Create /XML reads a task file as UTF-16. Every task definition
jacked writes, keeps as a backup, or compares is the same canonical UTF-16
LE bytes, whatever form schtasks /Query /XML printed it in.
"""

import codecs
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jacked.service.supervisors._task_xml import (
    canonical_task_xml,
    decode_task_xml,
    encode_task_xml,
    first_line,
    query_task_xml,
)

BODY = '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Description>José</Description></Task>'


def test_encode_writes_a_bom_and_a_matching_utf16_declaration():
    content = encode_task_xml(BODY)
    assert content.startswith(codecs.BOM_UTF16_LE)
    text = content[2:].decode("utf-16-le")
    assert text.startswith('<?xml version="1.0" encoding="UTF-16"?>')
    assert text.endswith(BODY)


def test_encode_replaces_an_existing_declaration_once():
    content = encode_task_xml("<?xml version='1.0' encoding='utf-8'?>\n" + BODY)
    text = content[2:].decode("utf-16-le")
    assert text.count("<?xml") == 1
    assert "utf-8" not in text


@pytest.mark.parametrize("raw", [
    # schtasks writing UTF-16 LE with a BOM to the pipe
    codecs.BOM_UTF16_LE + ('<?xml version="1.0" encoding="UTF-16"?>\r\n' + BODY).encode("utf-16-le"),
    # UTF-16 LE without a BOM
    ('<?xml version="1.0" encoding="UTF-16"?>\r\n' + BODY).encode("utf-16-le"),
    # UTF-16 BE with a BOM
    codecs.BOM_UTF16_BE + BODY.encode("utf-16-be"),
    # the UTF-8 artifact older jacked versions wrote
    ("<?xml version='1.0' encoding='utf-8'?>\n" + BODY).encode("utf-8"),
    codecs.BOM_UTF8 + BODY.encode("utf-8"),
    # text-mode stdout from a fake or an older caller
    '<?xml version="1.0" encoding="UTF-16"?>\r\n' + BODY,
])
def test_every_query_form_becomes_the_same_canonical_bytes(raw):
    from xml.etree import ElementTree

    canonical = canonical_task_xml(raw)
    assert canonical == encode_task_xml(BODY)
    description = next(ElementTree.fromstring(canonical).iter(
        "{http://schemas.microsoft.com/windows/2004/02/mit/task}Description"))
    assert description.text == "José"



def test_pure_ascii_console_text_is_exact():
    """8-bit text that declares UTF-16 but holds only ASCII decodes exactly."""
    body = BODY.replace("\u00e9", "e")
    raw = ('<?xml version="1.0" encoding="UTF-16"?>\r\n' + body).encode("ascii")
    assert canonical_task_xml(raw) == encode_task_xml(body)


@pytest.mark.parametrize("raw", [
    # UTF-8 bytes under a UTF-16 declaration (a console in a UTF-8 code page)
    ('<?xml version="1.0" encoding="UTF-16"?>\r\n' + BODY).encode("utf-8"),
    # CP850 "\u251c\u00ae" is C3 A9, the same bytes as UTF-8 "\u00e9": a
    # different path that a UTF-8-first decode would read as ours
    ('<?xml version="1.0" encoding="UTF-16"?>\r\n'
     "<Task><Arguments>C:\\Users\\\u251c\u00ae\\launcher.ps1</Arguments></Task>").encode("cp850"),
])
def test_non_ascii_console_text_is_refused_not_guessed(raw):
    """8-bit text that declares UTF-16 was re-encoded in an unknown code
    page. A non-ASCII byte there is ambiguous, so ownership fails closed."""
    with pytest.raises(UnicodeDecodeError):
        decode_task_xml(raw)

def test_decode_strips_the_bom_and_keeps_the_text():
    text = decode_task_xml(codecs.BOM_UTF16_LE + ('<?xml version="1.0" encoding="UTF-16"?>' + BODY).encode("utf-16-le"))
    assert text.lstrip().startswith("<?xml")  # decode keeps the text as printed
    assert not text.startswith("﻿")


def test_odd_length_utf16_is_refused_not_guessed():
    with pytest.raises(UnicodeDecodeError):
        decode_task_xml(codecs.BOM_UTF16_LE + b"<\x00T")


@pytest.mark.parametrize("value, expected", [
    ("", ""),
    (None, ""),
    ("\n\nERROR: The task XML is malformed.\n\n(1,40)::ERROR: unable to switch the encoding\n",
     "ERROR: The task XML is malformed."),
    (b"  ERROR: Access is denied.\r\n", "ERROR: Access is denied."),
])
def test_first_line(value, expected):
    assert first_line(value) == expected


class TestQueryTaskXml:
    def test_reads_the_com_query_as_utf8_and_returns_canonical_bytes(self):
        """The COM query writes UTF-8 text that still declares UTF-16; the
        bytes come back captured (not text mode) and canonical."""
        printed = ('<?xml version="1.0" encoding="UTF-16"?>\r\n' + BODY).encode("utf-8")
        run = Mock(return_value=SimpleNamespace(returncode=0, stdout=printed, stderr=b""))
        result = query_task_xml(run, "ai.hank.jacked", timeout=15)
        assert (result.returncode, result.absent) == (0, False)
        assert result.xml == encode_task_xml(BODY)
        argv = run.call_args.args[0]
        assert argv[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
        assert "GetTask('ai.hank.jacked')" in argv[4]
        kwargs = run.call_args.kwargs
        assert kwargs["capture_output"] is True and kwargs.get("text") is not True
        assert kwargs["timeout"] == 15 and kwargs["check"] is False

    def test_exit_3_is_an_absent_task(self):
        run = Mock(return_value=SimpleNamespace(returncode=3, stdout=b"", stderr=b""))
        result = query_task_xml(run, "ai.hank.jacked", timeout=15)
        assert result.absent and result.xml is None

    def test_any_other_failure_is_not_absent_and_keeps_stderr(self):
        run = Mock(return_value=SimpleNamespace(
            returncode=4, stdout=b"", stderr=b"Exception calling Connect\r\n"))
        result = query_task_xml(run, "ai.hank.jacked", timeout=15)
        assert not result.absent and result.xml is None
        assert result.stderr == "Exception calling Connect"

    def test_output_that_is_not_utf8_is_no_xml(self):
        run = Mock(return_value=SimpleNamespace(
            returncode=0, stdout="<Task>Jos\u00e9</Task>".encode("cp1252"), stderr=b""))
        assert query_task_xml(run, "ai.hank.jacked", timeout=15).xml is None
