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
    # 8-bit console text that still says UTF-16 (the declaration lies)
    ('<?xml version="1.0" encoding="UTF-16"?>\r\n' + BODY).encode("utf-8"),
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
    def test_registered_task_comes_back_canonical_and_bytes_are_captured(self):
        raw = codecs.BOM_UTF16_LE + ('<?xml version="1.0" encoding="UTF-16"?>' + BODY).encode("utf-16-le")
        run = Mock(return_value=SimpleNamespace(returncode=0, stdout=raw, stderr=b""))
        result = query_task_xml(run, "ai.hank.jacked", timeout=15)
        assert result.returncode == 0
        assert result.xml == encode_task_xml(BODY)
        argv = run.call_args.args[0]
        assert argv == ["schtasks.exe", "/Query", "/TN", "ai.hank.jacked", "/XML"]
        kwargs = run.call_args.kwargs
        assert kwargs["capture_output"] is True and kwargs.get("text") is not True
        assert kwargs["timeout"] == 15 and kwargs["check"] is False

    def test_absent_task_carries_no_xml_and_the_first_stderr_line(self):
        run = Mock(return_value=SimpleNamespace(
            returncode=1, stdout=b"", stderr=b"ERROR: The system cannot find the file specified.\r\n"))
        result = query_task_xml(run, "ai.hank.jacked", timeout=15)
        assert (result.returncode, result.xml) == (1, None)
        assert result.stderr == "ERROR: The system cannot find the file specified."

    def test_undecodable_output_is_no_xml(self):
        run = Mock(return_value=SimpleNamespace(
            returncode=0, stdout=codecs.BOM_UTF16_LE + b"<", stderr=b""))
        assert query_task_xml(run, "ai.hank.jacked", timeout=15).xml is None
