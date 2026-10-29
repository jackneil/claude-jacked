"""Typed native-manager autostart inspection tests."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

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
    run = Mock(return_value=SimpleNamespace(returncode=1, stdout=""))
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
    run = Mock(return_value=SimpleNamespace(returncode=1, stdout=""))
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


def test_windows_owned_task_printed_as_utf16_is_enabled(tmp_path):
    """#165i: the real query output declares UTF-16. A text-mode decode plus
    a UTF-8 re-encode used to make our own task read as unowned."""
    import codecs

    from jacked.service.spec import SupervisorKind
    from jacked.service.supervisors._task_xml import decode_task_xml
    from tests.unit.service.supervisor_test_support import installed_artifact

    _, _, rendered, _ = installed_artifact(tmp_path, SupervisorKind.TASK_SCHEDULER)
    printed = codecs.BOM_UTF16_LE + decode_task_xml(rendered.content).encode("utf-16-le")
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=printed, stderr=b""))

    result = inspect_autostart(
        platform="win32",
        run=run,
        task_path=tmp_path / "task.xml",
        legacy_vbs_path=tmp_path / "missing.vbs",
    )

    assert result.state is AutostartState.OWNED_ENABLED
    assert run.call_args.kwargs.get("text") is not True  # bytes, decoded by us
