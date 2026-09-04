import subprocess
from types import SimpleNamespace
from unittest.mock import Mock


from jacked.service.spec import SupervisorKind
from jacked.service.supervisors import (
    install_owned_supervisor,
)
from tests.unit.service.supervisor_test_support import (
    installed_artifact as _installed_artifact,
)


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
    previous = rendered.content.replace(
        spec.generation.encode(), b"0" * len(spec.generation)
    )
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
    previous = rendered.content.replace(
        spec.generation.encode(), b"0" * len(spec.generation)
    )
    path.write_bytes(previous)
    runner = Mock(
        side_effect=[
            SimpleNamespace(returncode=0, stdout=previous.decode()),
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
    previous = rendered.content.replace(
        spec.generation.encode(), b"0" * len(spec.generation)
    )
    path.write_bytes(previous)
    runner = Mock(
        side_effect=[
            SimpleNamespace(returncode=0, stdout=previous.decode()),
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
    monkeypatch.setenv("APPDATA", str(appdata))
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        if command[1:3] == ["/Query", "/TN"] and len(calls) == 1:
            return SimpleNamespace(returncode=1, stdout="")
        if command[1] == "/Create":
            raise subprocess.TimeoutExpired("schtasks", 15)
        if command[1] == "/Query":
            return SimpleNamespace(returncode=0, stdout=rendered.content.decode())
        if command[1] == "/Delete":
            assert not legacy.exists()
            return SimpleNamespace(returncode=0, stdout="")
        raise AssertionError(command)

    result = install_owned_supervisor(spec, path, environment=environment, run=run)

    assert result.ok is False
    assert "previous Task Scheduler state restored" in result.reason
    assert legacy.exists()
    assert [call[1] for call in calls] == ["/Query", "/Create", "/Query", "/Delete"]
