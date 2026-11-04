"""The dashboard's Upgrade button reports a refused start (header.js).

POST /api/upgrade now answers 403 (remote access policy) and 409 with
``{"detail": "Account switch in progress. ..."}`` (the switch lease). Only the
pre-existing 409 ``Upgrade already in progress`` may be swallowed, because
the running upgrade's WebSocket events drive the modal for that one.
"""

from pathlib import Path

HEADER_JS = Path(__file__).resolve().parents[2] / "jacked/data/web/js/components/header.js"
WEBSOCKET_JS = Path(__file__).resolve().parents[2] / "jacked/data/web/js/websocket.js"


def _start_upgrade_source() -> str:
    source = HEADER_JS.read_text(encoding="utf-8")
    start = source.index("async function startUpgrade()")
    end = source.index("function _getOrCreateUpgradeModal()")
    return source[start:end]


def test_only_the_running_upgrade_409_is_silent():
    body = _start_upgrade_source()
    assert "detail === 'Upgrade already in progress'" in body
    # The old blanket early return swallowed the lease 409 too.
    assert "if (e.status === 409) return;" not in body


def test_a_refused_start_shows_the_servers_reason_and_frees_the_button():
    body = _start_upgrade_source()
    assert "_showUpgradeError('Failed to start upgrade: ' + (detail || e.message))" in body
    assert "btn.disabled = false" in body


def test_a_failed_upgrade_event_frees_the_button_too():
    """The route can accept and the task still refuse (a switch takes the
    lease in between): the failure arrives over the WebSocket, and the button
    must come back there as well."""
    source = WEBSOCKET_JS.read_text(encoding="utf-8")
    start = source.index("jackedWS.on('upgrade_failed'")
    end = source.index("});", start)
    handler = source[start:end]
    assert "_showUpgradeError(d.error || 'Upgrade failed')" in handler
    assert "document.getElementById('version-upgrade-btn')" in handler
    assert "btn.disabled = false" in handler


def test_a_no_op_upgrade_shows_a_note_not_an_error():
    """#295i: an index that serves an older build is "nothing to upgrade".
    The page says so as a note (no error style) and gives the button back."""
    source = WEBSOCKET_JS.read_text(encoding="utf-8")
    start = source.index("jackedWS.on('upgrade_noop'")
    end = source.index("});", start)
    handler = source[start:end]
    assert "_showUpgradeNote(" in handler
    assert "_showUpgradeError" not in handler
    assert "btn.disabled = false" in handler
    header = HEADER_JS.read_text(encoding="utf-8")
    note = header[header.index("function _showUpgradeNote("):]
    note = note[: note.index("\n}\n") + 3]
    assert "_showUpgradeMessage(message, false)" in note
