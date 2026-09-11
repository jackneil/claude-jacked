"""Tests for ``generateUuid`` in the dashboard's shared utils.

``crypto.randomUUID`` is a secure-context-only API. The dashboard is
reachable over plain http on a hostname (or Tailscale IP) when remote
access is on, so ``crypto.randomUUID`` is ``undefined`` there and calling
it throws a ``TypeError``. ``generateUuid`` must fall back to
``crypto.getRandomValues``, which is available in every context.

The fallback branch is also executed for real with ``node`` (skipped when
node is not on PATH) to prove it emits valid, unique RFC 4122 v4 UUIDs.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[2] / "jacked" / "data" / "web"
UTILS_JS = WEB / "js" / "utils.js"
INDEX_HTML = WEB / "index.html"

UUID_V4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def _generate_uuid_source() -> str:
    """Return the text of the ``generateUuid`` declaration in utils.js."""
    source = UTILS_JS.read_text()
    start = source.index("function generateUuid(")
    depth = 0
    for i in range(source.index("{", start), len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start : i + 1]
    raise AssertionError("generateUuid() body is not brace-balanced")


def test_generate_uuid_is_defined_with_a_secure_context_guard() -> None:
    body = _generate_uuid_source()

    assert "typeof crypto.randomUUID === 'function'" in body
    assert "crypto.randomUUID()" in body


def test_generate_uuid_falls_back_to_get_random_values_with_v4_bits() -> None:
    body = _generate_uuid_source()

    assert "crypto.getRandomValues(new Uint8Array(16))" in body
    assert "0x40" in body  # version nibble
    assert "0x80" in body  # variant bits


def test_utils_js_documents_why_the_fallback_exists() -> None:
    source = UTILS_JS.read_text()
    preamble = source[: source.index("function generateUuid(")].rstrip().splitlines()
    comment = []
    for line in reversed(preamble):
        if not line.strip().startswith("//"):
            break
        comment.append(line)
    doc = "\n".join(comment).lower()

    assert "secure context" in doc
    assert "http" in doc


def test_utils_js_loads_before_every_component_script() -> None:
    html = INDEX_HTML.read_text()
    srcs = re.findall(r'<script[^>]*\ssrc="([^"]+)"', html)
    local = [s for s in srcs if s.startswith("/js/")]

    assert "/js/utils.js" in local
    utils_at = local.index("/js/utils.js")
    components = [i for i, s in enumerate(local) if s.startswith("/js/components/")]
    assert components, "no component scripts found in index.html"
    assert utils_at < min(components)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_generate_uuid_fallback_emits_valid_unique_v4_uuids() -> None:
    """Run the real fallback branch under node, with randomUUID absent."""
    program = """
const { webcrypto } = require('node:crypto');
// A crypto object WITHOUT randomUUID is exactly what a non-secure context
// hands the page, so this forces generateUuid down its fallback branch.
const insecureCrypto = { getRandomValues: (a) => webcrypto.getRandomValues(a) };
const out = (function (crypto) {
__GENERATE_UUID__
    const values = [];
    for (let i = 0; i < 200; i++) values.push(generateUuid());
    return values;
})(insecureCrypto);
console.log(JSON.stringify(out));
""".replace("__GENERATE_UUID__", _generate_uuid_source())
    proc = subprocess.run(
        ["node", "-e", program], capture_output=True, text=True, check=True
    )
    uuids = json.loads(proc.stdout)

    assert len(uuids) == 200
    for value in uuids:
        assert UUID_V4_RE.match(value), value
    assert len(set(uuids)) == 200
