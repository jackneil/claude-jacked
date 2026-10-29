"""Task Scheduler XML: one encoding for every write, read and compare (#165i).

``schtasks /Create /XML`` reads the file as UTF-16. A UTF-8 file fails with
"unable to switch the encoding". So every task definition that jacked
writes, keeps as a backup, or compares is the same canonical bytes: UTF-16
LE with a byte-order mark and a matching declaration.

A registered task is read through the Task Scheduler COM API, never
through ``schtasks /Query /XML``: piped, schtasks writes the console code
page, and the Windows CI round trip proved a non-ASCII launcher path
(C:\\Users\\Jos\u00e9) comes back as 8-bit text that still declares UTF-16, which
cannot be decoded exactly. ``query_task_xml`` runs ``task_query_command``,
which writes the definition as UTF-8.
"""

from __future__ import annotations

import codecs
import locale
import re
import sys
from dataclasses import dataclass
from typing import Any

TASK_XML_DECLARATION = '<?xml version="1.0" encoding="UTF-16"?>\r\n'

_DECLARATION = re.compile(r"\A\s*<\?xml[^>]*\?>\s*")
_DECLARED_ENCODING = re.compile(rb"""\A\s*<\?xml[^>]*?encoding\s*=\s*["']([^"']+)["']""")

_BOMS = (
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
    (codecs.BOM_UTF8, "utf-8"),
)


def encode_task_xml(text: str) -> bytes:
    """Return ``text`` as canonical task bytes (BOM + UTF-16 declaration).

    >>> encode_task_xml("<Task/>")[:4]
    b'\\xff\\xfe<\\x00'
    >>> encode_task_xml("<?xml version='1.0'?><Task/>") == encode_task_xml("<Task/>")
    True
    """
    body = _DECLARATION.sub("", text.lstrip("﻿"), count=1)
    return codecs.BOM_UTF16_LE + (TASK_XML_DECLARATION + body).encode("utf-16-le")


def decode_task_xml(raw: bytes | str) -> str:
    """Decode task XML from any form schtasks or jacked produced.

    UTF-16 (with a byte-order mark, or recognized by its NUL bytes) and
    UTF-8 (a UTF-8 or undeclared document: the artifact older jacked
    versions wrote) decode exactly. 8-bit text that declares UTF-16 was
    re-encoded by a console in an unknown code page: it is accepted only
    when it is pure ASCII, because a non-ASCII byte could stand for a
    different character, and the Arguments path decides ownership.

    Raises ``UnicodeDecodeError`` for bytes that do not decode exactly,
    never a guess.

    >>> decode_task_xml("<Task/>".encode("utf-16-le"))
    '<Task/>'
    >>> decode_task_xml(b"<Task/>")
    '<Task/>'
    >>> lying = '<?xml version="1.0" encoding="UTF-16"?><T>\u00e9</T>'.encode("utf-8")
    >>> try:
    ...     decode_task_xml(lying)
    ... except UnicodeDecodeError:
    ...     print("refused")
    refused
    """
    if isinstance(raw, str):
        return raw.lstrip("\ufeff")
    for bom, codec in _BOMS:
        if raw.startswith(bom):
            return raw[len(bom):].decode(codec)
    if len(raw) >= 2 and raw[1] == 0:
        return raw.decode("utf-16-le")
    if len(raw) >= 2 and raw[0] == 0:
        return raw.decode("utf-16-be")
    if _declares_utf16(raw):
        return raw.decode("ascii")
    return raw.decode("utf-8")


def _declares_utf16(raw: bytes) -> bool:
    match = _DECLARED_ENCODING.match(raw)
    return bool(match) and match.group(1).lower().replace(b"_", b"-") in {
        b"utf-16", b"utf16", b"ucs-2", b"unicode"
    }


def canonical_task_xml(raw: bytes | str) -> bytes:
    """Decode ``raw`` and re-encode it as canonical task bytes."""
    return encode_task_xml(decode_task_xml(raw))


def first_line(value: bytes | str | None) -> str:
    """The first non-empty line of a command's stderr, stripped.

    >>> first_line("\\nERROR: Access is denied.\\r\\nmore")
    'ERROR: Access is denied.'
    """
    if not value:
        return ""
    text = value if isinstance(value, str) else _decode_console(value)
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


# task_query_command exit codes: the task does not exist (HRESULT
# 0x80070002, the same in every Windows language), or any other failure.
TASK_ABSENT_EXIT = 3
TASK_QUERY_FAILED_EXIT = 4


@dataclass(frozen=True)
class TaskQuery:
    returncode: int
    xml: bytes | None  # canonical task bytes; None when absent or undecodable
    stderr: str  # first non-empty stderr line

    @property
    def absent(self) -> bool:
        return self.returncode == TASK_ABSENT_EXIT


def task_query_command(service_id: str) -> list[str]:
    """PowerShell that writes one task's XML, as UTF-8, from the COM API.

    Exit 0 with the definition on stdout, ``TASK_ABSENT_EXIT`` when the task
    does not exist, ``TASK_QUERY_FAILED_EXIT`` on any other failure.

    >>> command = task_query_command("ai.hank.jacked")
    >>> command[0], "UTF8Encoding" in command[-1], "GetTask('ai.hank.jacked')" in command[-1]
    ('powershell.exe', True, True)
    >>> "GetTask('it''s')" in task_query_command("it's")[-1]
    True
    """
    task_name = service_id.replace("'", "''")
    script = (
        "$ErrorActionPreference='Stop';"
        "[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding $false;"
        "$service=New-Object -ComObject 'Schedule.Service';"
        "$service.Connect();$folder=$service.GetFolder('\\');"
        f"try{{$task=$folder.GetTask('{task_name}')}}"
        "catch{if($_.Exception.HResult -eq -2147024894)"
        f"{{exit {TASK_ABSENT_EXIT}}}else{{exit {TASK_QUERY_FAILED_EXIT}}}}};"
        "[Console]::Out.Write($task.Xml)"
    )
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script]


def query_task_xml(run: Any, service_id: str, *, timeout: float) -> TaskQuery:
    """Read one registered task and return its canonical bytes.

    The output is captured as bytes and decoded as the UTF-8 the script
    writes (text mode would decode it with the locale code page). Output
    that is not valid UTF-8 gives ``xml=None``, which every ownership
    decision treats as "not ours".
    """
    result = run(
        task_query_command(service_id),
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    xml = None
    if result.returncode == 0 and result.stdout:
        raw = result.stdout
        try:
            text = raw if isinstance(raw, str) else raw.decode("utf-8-sig")
            xml = encode_task_xml(text)
        except UnicodeDecodeError:
            xml = None
    return TaskQuery(result.returncode, xml, first_line(getattr(result, "stderr", None)))


def _decode_console(raw: bytes) -> str:
    """Best-effort text of a command's message output (never XML)."""
    if raw[:2] in (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE):
        return raw.decode("utf-16", errors="replace")
    if len(raw) >= 2 and raw[1] == 0:
        return raw.decode("utf-16-le", errors="replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode(_console_encoding(), errors="replace")


def _console_encoding() -> str:
    """The code page Windows console tools write to a pipe (OEM)."""
    if sys.platform == "win32":
        try:
            codecs.lookup("oem")
            return "oem"
        except LookupError:
            pass
    return locale.getpreferredencoding(False) or "utf-8"
