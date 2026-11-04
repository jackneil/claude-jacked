"""Typed native-manager autostart inspection tests."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from jacked.service.autostart import AutostartState, inspect_autostart


_OWNED_MARKER = {"owner": "claude-jacked", "service_id": "ai.hank.jacked"}


def test_windows_registered_owned_task_is_enabled(tmp_path):
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="<Task />"))
    with patch("jacked.service.autostart.extract_marker", return_value=_OWNED_MARKER):
        result = inspect_autostart(
            platform="win32",
            run=run,
            task_path=tmp_path / "task.xml",
            legacy_vbs_path=tmp_path / "missing.vbs",
        )
    assert result.state is AutostartState.OWNED_ENABLED
    assert result.enabled is True


def test_windows_registered_owned_disabled_task_is_disabled(tmp_path):
    run = Mock(
        return_value=SimpleNamespace(
            returncode=0,
            stdout="<Task><Settings><Enabled>false</Enabled></Settings></Task>",
        )
    )
    with patch("jacked.service.autostart.extract_marker", return_value=_OWNED_MARKER):
        result = inspect_autostart(
            platform="win32",
            run=run,
            task_path=tmp_path / "task.xml",
            legacy_vbs_path=tmp_path / "missing.vbs",
        )
    assert result.state is AutostartState.OWNED_DISABLED


def test_windows_task_plus_legacy_vbs_is_duplicate(tmp_path):
    legacy = tmp_path / "jacked.vbs"
    legacy.write_text("legacy", encoding="utf-8")
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="<Task />"))
    with patch("jacked.service.autostart.extract_marker", return_value=_OWNED_MARKER):
        result = inspect_autostart(
            platform="win32",
            run=run,
            task_path=tmp_path / "task.xml",
            legacy_vbs_path=legacy,
        )
    assert result.state is AutostartState.DUPLICATE
    assert result.toggle_safe is False


def test_windows_staged_definition_is_not_enabled(tmp_path):
    task = tmp_path / "task.xml"
    task.write_text("<Task />", encoding="utf-8")
    run = Mock(return_value=SimpleNamespace(returncode=3, stdout=""))
    with patch("jacked.service.autostart.extract_marker", return_value=_OWNED_MARKER):
        result = inspect_autostart(
            platform="win32",
            run=run,
            task_path=task,
            legacy_vbs_path=tmp_path / "missing.vbs",
        )
    assert result.state is AutostartState.OWNED_DISABLED
    assert result.enabled is False


def test_windows_staged_foreign_definition_is_foreign(tmp_path):
    task = tmp_path / "task.xml"
    task.write_text("<Task />", encoding="utf-8")
    run = Mock(return_value=SimpleNamespace(returncode=3, stdout=""))
    with patch("jacked.service.autostart.extract_marker", return_value=None):
        result = inspect_autostart(
            platform="win32",
            run=run,
            task_path=task,
            legacy_vbs_path=tmp_path / "missing.vbs",
        )
    assert result.state is AutostartState.FOREIGN


def test_macos_owned_disabled_override_is_not_enabled(tmp_path):
    plist = tmp_path / "ai.hank.jacked.plist"
    plist.write_bytes(b"owned")
    run = Mock(
        return_value=SimpleNamespace(
            returncode=0,
            stdout='disabled services = {\n"ai.hank.jacked" => true\n}',
        )
    )
    with patch("jacked.service.autostart.extract_marker", return_value=_OWNED_MARKER):
        result = inspect_autostart(
            platform="darwin", run=run, launchd_path=plist
        )
    assert result.state is AutostartState.OWNED_DISABLED


def test_linux_unit_requires_manager_enabled_state(tmp_path):
    unit = tmp_path / "jacked.service"
    unit.write_text("owned", encoding="utf-8")
    with patch("jacked.service.autostart.extract_marker", return_value=_OWNED_MARKER):
        disabled = inspect_autostart(
            platform="linux",
            run=Mock(return_value=SimpleNamespace(returncode=1, stdout="disabled")),
            systemd_path=unit,
        )
        enabled = inspect_autostart(
            platform="linux",
            run=Mock(return_value=SimpleNamespace(returncode=0, stdout="enabled")),
            systemd_path=unit,
        )
    assert disabled.state is AutostartState.OWNED_DISABLED
    assert enabled.state is AutostartState.OWNED_ENABLED


def test_windows_owned_task_read_back_as_utf8_is_enabled(tmp_path):
    """#165i: the read-back XML still declares UTF-16 while its bytes are
    UTF-8. A plain re-encode used to make our own task read as unowned."""
    from jacked.service.spec import SupervisorKind
    from jacked.service.supervisors._task_xml import decode_task_xml
    from tests.unit.service.supervisor_test_support import installed_artifact

    _, _, rendered, _ = installed_artifact(tmp_path, SupervisorKind.TASK_SCHEDULER)
    # The COM query writes the definition as UTF-8 text (declaration and all).
    printed = decode_task_xml(rendered.content).encode("utf-8")
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=printed, stderr=b""))

    result = inspect_autostart(
        platform="win32",
        run=run,
        task_path=tmp_path / "task.xml",
        legacy_vbs_path=tmp_path / "missing.vbs",
    )

    assert result.state is AutostartState.OWNED_ENABLED
    assert run.call_args.kwargs.get("text") is not True  # bytes, decoded by us


_LIST_MARKER_TASK = (
    '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
    "<RegistrationInfo><Description>JACKED-MARKER:[1]</Description>"
    "</RegistrationInfo></Task>"
)


@pytest.mark.parametrize("registered", [True, False], ids=["registered", "staged"])
def test_windows_task_marker_that_is_not_an_object_is_unowned(tmp_path, registered):
    """The marker JSON parsed to a list and the owner check called .get on
    it, so the inspection raised AttributeError instead of an answer."""
    task = tmp_path / "task.xml"
    if registered:
        run = Mock(
            return_value=SimpleNamespace(
                returncode=0, stdout=_LIST_MARKER_TASK.encode("utf-8"), stderr=b""
            )
        )
    else:
        task.write_text(_LIST_MARKER_TASK, encoding="utf-8")
        run = Mock(return_value=SimpleNamespace(returncode=3, stdout=b"", stderr=b""))

    result = inspect_autostart(
        platform="win32",
        run=run,
        task_path=task,
        legacy_vbs_path=tmp_path / "missing.vbs",
    )

    assert result.state is AutostartState.FOREIGN
    assert result.reason == (
        "registered task is unowned" if registered else "staged task is unowned"
    )
    assert result.generation is None


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(
            b'<?xml version="1.0" encoding="UTF-8"?>\n'
            b'<plist version="1.0"><dict><key>Label</key>',
            id="truncated",
        ),
        pytest.param(
            b'<plist version="1.0"><array><string>ai.hank.jacked</string></array></plist>',
            id="array-root",
        ),
    ],
)
def test_macos_plist_that_does_not_parse_as_a_dictionary_is_foreign(tmp_path, content):
    """Neither shape is ours, and neither may raise out of the inspection."""
    plist = tmp_path / "ai.hank.jacked.plist"
    plist.write_bytes(content)
    run = Mock(side_effect=AssertionError("an unowned plist needs no launchctl"))

    result = inspect_autostart(platform="darwin", run=run, launchd_path=plist)

    assert result.state is AutostartState.FOREIGN
    assert result.reason == "unowned launchd definition"
    run.assert_not_called()
