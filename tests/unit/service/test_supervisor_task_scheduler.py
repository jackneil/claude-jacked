import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


from jacked.service.spec import SupervisorKind
from jacked.service.supervisors import (
    install_owned_supervisor,
)
from jacked.service.supervisors._task_xml import decode_task_xml, encode_task_xml
from tests.unit.service.supervisor_test_support import (
    _secure_test_path,
    installed_artifact as _installed_artifact,
)


def _previous_generation(content: bytes, generation: str) -> bytes:
    """The same task at an older generation (the artifact is UTF-16 text)."""
    previous = encode_task_xml(
        decode_task_xml(content).replace(generation, "0" * len(generation))
    )
    assert previous != content
    return previous


def test_install_task_refuses_while_legacy_startup_vbs_exists(tmp_path, monkeypatch):
    spec, environment, _, path = _installed_artifact(
        tmp_path, SupervisorKind.TASK_SCHEDULER
    )
    appdata = tmp_path / "AppData" / "Roaming"
    startup = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    startup.mkdir(parents=True)
    legacy = startup / "jacked.vbs"
    legacy.write_text("legacy", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(appdata))
    runner = Mock()

    result = install_owned_supervisor(spec, path, environment=environment, run=runner)

    assert result.ok is False
    assert "Startup VBS identity is foreign" in result.reason
    runner.assert_not_called()


def test_install_task_retires_known_legacy_vbs_and_runs_task(tmp_path, monkeypatch):
    spec, environment, _, path = _installed_artifact(
        tmp_path, SupervisorKind.TASK_SCHEDULER
    )
    appdata = tmp_path / "AppData" / "Roaming"
    legacy = (
        appdata
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        / "jacked.vbs"
    )
    legacy.parent.mkdir(parents=True)
    legacy_content = (
        'Set WshShell = CreateObject("WScript.Shell")\n'
        'WshShell.Run """C:\\bin\\jacked.exe"" service start'
        ' --port 8321", 0, False\n'
    )
    legacy.write_text(legacy_content, encoding="utf-8")
    _secure_test_path(legacy)
    monkeypatch.setenv("APPDATA", str(appdata))
    runner = Mock(
        side_effect=[
            SimpleNamespace(returncode=1, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
        ]
    )

    result = install_owned_supervisor(spec, path, environment=environment, run=runner)

    assert result.ok is True
    assert not legacy.exists()
    assert legacy.with_name("jacked.vbs.pre-v2").read_text() == legacy_content
    assert runner.call_args_list[1].args[0][:2] == ["schtasks.exe", "/Create"]
    assert runner.call_args_list[2].args[0] == [
        "schtasks.exe",
        "/Run",
        "/TN",
        "ai.hank.jacked",
    ]


def test_install_task_foreign_registered_definition_never_changes_disk(tmp_path):
    spec, environment, rendered, path = _installed_artifact(
        tmp_path, SupervisorKind.TASK_SCHEDULER
    )
    previous = _previous_generation(rendered.content, spec.generation)
    path.write_bytes(previous)
    runner = Mock(return_value=SimpleNamespace(returncode=0, stdout="<Task />"))

    result = install_owned_supervisor(spec, path, environment=environment, run=runner)

    assert result.ok is False
    assert "registered task is foreign" in result.reason
    assert path.read_bytes() == previous
    assert runner.call_count == 1


def test_install_task_run_failure_restores_stopped_task_and_artifact(tmp_path):
    spec, environment, rendered, path = _installed_artifact(
        tmp_path, SupervisorKind.TASK_SCHEDULER
    )
    previous = _previous_generation(rendered.content, spec.generation)
    path.write_bytes(previous)
    runner = Mock(
        side_effect=[
            SimpleNamespace(returncode=0, stdout=decode_task_xml(previous)),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=1, stdout=""),
            SimpleNamespace(returncode=5, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
        ]
    )

    result = install_owned_supervisor(spec, path, environment=environment, run=runner)

    assert result.ok is False
    assert "previous Task Scheduler state restored" in result.reason
    assert path.read_bytes() == previous
    assert runner.call_count == 5
    assert not path.with_name(f".{path.name}.transition-backup").exists()


def test_install_task_reports_and_retains_failed_rollback_evidence(tmp_path):
    spec, environment, rendered, path = _installed_artifact(
        tmp_path, SupervisorKind.TASK_SCHEDULER
    )
    previous = _previous_generation(rendered.content, spec.generation)
    path.write_bytes(previous)
    runner = Mock(
        side_effect=[
            SimpleNamespace(returncode=0, stdout=decode_task_xml(previous)),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=5, stdout=""),
            SimpleNamespace(returncode=5, stdout=""),
        ]
    )

    result = install_owned_supervisor(spec, path, environment=environment, run=runner)

    backup = path.with_name(f".{path.name}.transition-backup")
    assert result.ok is False
    assert "rollback failed" in result.reason
    assert str(backup) in result.reason
    assert backup.read_bytes() == previous
    assert path.read_bytes() == previous


def test_ambiguous_task_create_is_deleted_before_legacy_vbs_returns(
    tmp_path, monkeypatch
):
    spec, environment, rendered, path = _installed_artifact(
        tmp_path, SupervisorKind.TASK_SCHEDULER
    )
    appdata = tmp_path / "AppData" / "Roaming"
    legacy = (
        appdata
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        / "jacked.vbs"
    )
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        'Set WshShell = CreateObject("WScript.Shell")\n'
        'WshShell.Run """C:\\bin\\jacked.exe"" service start'
        ' --port 8321", 0, False\n',
        encoding="utf-8",
    )
    _secure_test_path(legacy)
    monkeypatch.setenv("APPDATA", str(appdata))
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        if command[1:3] == ["/Query", "/TN"] and len(calls) == 1:
            return SimpleNamespace(returncode=1, stdout="")
        if command[1] == "/Create":
            raise subprocess.TimeoutExpired("schtasks", 15)
        if command[1] == "/Query":
            return SimpleNamespace(returncode=0, stdout=decode_task_xml(rendered.content))
        if command[1] == "/Delete":
            assert not legacy.exists()
            return SimpleNamespace(returncode=0, stdout="")
        raise AssertionError(command)

    result = install_owned_supervisor(spec, path, environment=environment, run=run)

    assert result.ok is False
    assert "previous Task Scheduler state restored" in result.reason
    assert legacy.exists()
    assert [call[1] for call in calls] == ["/Query", "/Create", "/Query", "/Delete"]


def test_is_known_legacy_artifact_recognises_the_pre_v2_startup_vbs(tmp_path):
    from jacked.service.supervisors import is_known_legacy_artifact

    legacy = tmp_path / "jacked.vbs"
    legacy.write_text(
        'Set WshShell = CreateObject("WScript.Shell")\n'
        'WshShell.Run """C:\\bin\\jacked.exe"" service start --port 8321", 0, False\n',
        encoding="utf-8",
    )
    _secure_test_path(legacy)

    assert is_known_legacy_artifact(
        legacy, "ai.hank.jacked", SupervisorKind.TASK_SCHEDULER
    )


def test_is_known_legacy_artifact_rejects_a_foreign_startup_vbs(tmp_path):
    from jacked.service.supervisors import is_known_legacy_artifact

    legacy = tmp_path / "jacked.vbs"
    legacy.write_text('WshShell.Run "format c:", 0, False\n', encoding="utf-8")
    _secure_test_path(legacy)

    assert not is_known_legacy_artifact(
        legacy, "ai.hank.jacked", SupervisorKind.TASK_SCHEDULER
    )


def test_is_known_legacy_artifact_rejects_the_owned_task_xml(tmp_path):
    from jacked.service.supervisors import is_known_legacy_artifact

    spec, _environment, _rendered, path = _installed_artifact(
        tmp_path, SupervisorKind.TASK_SCHEDULER
    )

    assert not is_known_legacy_artifact(
        path, spec.service_id, SupervisorKind.TASK_SCHEDULER
    )


# --- #165i: schtasks reads task XML as UTF-16 -------------------------------

_MALFORMED = (
    "ERROR: The task XML is malformed.\n\n"
    "(1,40)::ERROR: unable to switch the encoding\n"
)


def _utf16_query_output(content: bytes) -> bytes:
    """Task XML the way schtasks can print it: UTF-16 LE with a BOM."""
    import codecs

    return codecs.BOM_UTF16_LE + decode_task_xml(content).encode("utf-16-le")


def test_rendered_task_is_utf16_with_a_matching_declaration(tmp_path):
    import codecs
    from xml.etree import ElementTree

    _, _, rendered, _ = _installed_artifact(tmp_path, SupervisorKind.TASK_SCHEDULER)
    assert rendered.content.startswith(codecs.BOM_UTF16_LE)
    text = rendered.content.decode("utf-16")
    assert text.startswith('<?xml version="1.0" encoding="UTF-16"?>')
    assert ElementTree.fromstring(rendered.content).tag.endswith("Task")


def test_create_failure_names_the_step_and_the_schtasks_message(tmp_path):
    spec, environment, _, path = _installed_artifact(
        tmp_path, SupervisorKind.TASK_SCHEDULER
    )
    runner = Mock(
        side_effect=[
            SimpleNamespace(returncode=1, stdout=b"", stderr=b""),
            SimpleNamespace(returncode=1, stdout="", stderr=_MALFORMED),
            SimpleNamespace(returncode=1, stdout=b"", stderr=b""),
        ]
    )

    result = install_owned_supervisor(spec, path, environment=environment, run=runner)

    assert result.ok is False
    assert result.reason.startswith(
        "schtasks /Create exit 1: ERROR: The task XML is malformed."
    )
    assert "previous Task Scheduler state restored" in result.reason


def test_a_registered_task_printed_as_utf16_is_owned_and_its_backup_restores(tmp_path):
    """The second install reads our own task back. Its backup must be a file
    schtasks /Create accepts: canonical UTF-16, never re-encoded UTF-8."""
    from xml.etree import ElementTree

    from jacked.service.supervisors import extract_marker

    spec, environment, rendered, path = _installed_artifact(
        tmp_path, SupervisorKind.TASK_SCHEDULER
    )
    previous = _previous_generation(rendered.content, spec.generation)
    path.write_bytes(previous)
    restored_from = []

    def run(argv, **kwargs):
        if argv[1] == "/Query":
            return SimpleNamespace(returncode=0, stdout=_utf16_query_output(previous), stderr=b"")
        if argv[1] == "/Create" and ".transition-backup" in argv[5]:
            restored_from.append(Path(argv[5]).read_bytes())
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[1] == "/Run" and not restored_from:
            return SimpleNamespace(returncode=1, stdout="", stderr="ERROR: Access is denied.\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = install_owned_supervisor(spec, path, environment=environment, run=run)

    assert result.ok is False
    assert result.reason.startswith("schtasks /Run exit 1: ERROR: Access is denied.")
    assert "previous Task Scheduler state restored" in result.reason
    assert restored_from == [previous]  # the bytes schtasks will accept
    marker = extract_marker(restored_from[0], SupervisorKind.TASK_SCHEDULER)
    assert marker["generation"] == "0" * len(spec.generation)
    assert ElementTree.fromstring(restored_from[0]).tag.endswith("Task")


def test_a_utf8_artifact_from_an_older_jacked_is_replaced(tmp_path):
    spec, environment, rendered, path = _installed_artifact(
        tmp_path, SupervisorKind.TASK_SCHEDULER
    )
    body = decode_task_xml(rendered.content).split("?>", 1)[1].lstrip()
    legacy = ("<?xml version='1.0' encoding='utf-8'?>\n" + body).encode("utf-8")
    path.write_bytes(legacy)
    runner = Mock(
        side_effect=[
            SimpleNamespace(returncode=1, stdout=b"", stderr=b""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        ]
    )

    result = install_owned_supervisor(spec, path, environment=environment, run=runner)

    assert result.ok is True, result.reason
    assert path.read_bytes() == rendered.content
