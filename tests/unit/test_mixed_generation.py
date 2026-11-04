"""An interpreter mid-upgrade keeps importing the new release's modules.

An upgrade replaces the package on disk under a running process. The
0.110.2 upgrade to 0.111.0 died twice on the same class: the CLI path at its
restart step (the new `jacked.service.supervisors` package) and the tray
path at its settings migration (the new `jacked.proctree`), both needing
`system_program`, a name 0.111.0 added to `jacked.winproc`, while the
process still held the 0.110.2 module. These tests pin both halves of the
fix: `names_from` reloading a held module that lacks a name, and both
transactions importing their modules before the install.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

import jacked
from jacked import mixed_generation


class TestStaleness:
    def test_a_normal_interpreter_is_not_stale(self):
        assert mixed_generation.loaded_version() == jacked.__version__
        assert mixed_generation.installed_version() == jacked.__version__
        assert mixed_generation.interpreter_is_stale() is False

    def test_a_loaded_release_that_differs_from_the_disk_is_stale(self, monkeypatch):
        monkeypatch.setattr(jacked, "__version__", "0.0.0")
        assert mixed_generation.interpreter_is_stale() is True

    def test_an_unknown_version_never_counts_as_stale(self, monkeypatch):
        monkeypatch.setattr(jacked, "__version__", "")
        assert mixed_generation.interpreter_is_stale() is False
        monkeypatch.setattr(jacked, "__version__", "0.0.0")
        monkeypatch.setattr(mixed_generation, "installed_version", lambda: "")
        assert mixed_generation.interpreter_is_stale() is False


class TestReloadMissing:
    def test_a_module_with_every_name_is_left_alone(self, monkeypatch):
        calls = []
        monkeypatch.setattr(importlib, "reload", lambda module: calls.append(module.__name__))
        assert mixed_generation.reload_missing("jacked.winproc", "system_program") is False
        assert calls == []

    def test_a_module_that_is_not_loaded_is_left_alone(self, monkeypatch):
        calls = []
        monkeypatch.setattr(importlib, "reload", lambda module: calls.append(module.__name__))
        monkeypatch.delitem(sys.modules, "jacked.never_imported_module", raising=False)
        assert mixed_generation.reload_missing("jacked.never_imported_module", "x") is False
        assert calls == []

    def test_a_reload_puts_the_on_disk_names_into_the_held_module(self, monkeypatch):
        """The module object stays the same, so every earlier import sees the names."""
        import jacked.winproc as winproc

        monkeypatch.delattr(winproc, "system_program")
        assert not hasattr(winproc, "system_program")
        assert mixed_generation.reload_missing("jacked.winproc", "system_program") is True
        assert sys.modules["jacked.winproc"] is winproc
        assert callable(winproc.system_program)


class TestNamesFrom:
    def test_a_current_module_is_read_without_a_reload(self, monkeypatch):
        import jacked.winproc as winproc

        calls = []
        monkeypatch.setattr(importlib, "reload", lambda module: calls.append(module.__name__))
        no_window, program = mixed_generation.names_from("jacked.winproc", "NO_WINDOW", "system_program")
        assert no_window is winproc.NO_WINDOW and program is winproc.system_program
        assert calls == []

    def test_a_held_module_that_lacks_a_name_is_reloaded_once(self, monkeypatch):
        import jacked.winproc as winproc

        monkeypatch.delattr(winproc, "system_program")
        (program,) = mixed_generation.names_from("jacked.winproc", "system_program")
        assert callable(program) and winproc.system_program is program


def _forget(monkeypatch, module: str) -> None:
    """Drop ``module`` and its submodules from ``sys.modules`` AND from the
    parent package attributes, so a re-import leaves no orphan bound on the
    package after monkeypatch restores ``sys.modules``."""
    for name in [n for n in sys.modules if n == module or n.startswith(module + ".")]:
        parent, _, leaf = name.rpartition(".")
        if parent in sys.modules:
            monkeypatch.delattr(sys.modules[parent], leaf, raising=False)
        monkeypatch.delitem(sys.modules, name)


@pytest.mark.parametrize("module", ["jacked.service.supervisors", "jacked.proctree", "jacked.service.process"])
class TestNewModulesImportInAStaleInterpreter:
    """The three modules an older upgrade process first-imports after the install."""

    def test_the_module_imports_when_the_held_winproc_predates_it(self, monkeypatch, module):
        import jacked.winproc as winproc

        monkeypatch.delattr(winproc, "system_program")
        _forget(monkeypatch, module)
        loaded = importlib.import_module(module)
        assert loaded.system_program is winproc.system_program
        assert callable(winproc.system_program)

    def test_a_current_interpreter_never_reloads(self, monkeypatch, module):
        calls = []
        monkeypatch.setattr(importlib, "reload", lambda m: calls.append(m.__name__))
        _forget(monkeypatch, module)
        importlib.import_module(module)
        assert calls == []


class TestPreloadUpgradeModules:
    def test_the_roots_and_the_supervisor_submodules_are_loaded(self, monkeypatch):
        import jacked.service as service_package

        for name in mixed_generation.UPGRADE_MODULES:
            monkeypatch.delitem(sys.modules, name, raising=False)
            monkeypatch.delattr(service_package, name.rsplit(".", 1)[1], raising=False)
        monkeypatch.delattr(jacked, "proctree", raising=False)
        loaded = mixed_generation.preload_upgrade_modules()
        assert loaded == tuple(sorted(loaded))
        for name in mixed_generation.UPGRADE_MODULES + (
            "jacked.service.supervisors.launchd",
            "jacked.service.supervisors.systemd",
            "jacked.service.supervisors.task_scheduler",
            "jacked.service.supervisors.uninstall",
            "jacked.service.update_status",
            "jacked.service.ipc",
        ):
            assert name in sys.modules, name

    def test_lazy_imports_reads_function_bodies_and_named_submodules(self):
        found = mixed_generation.lazy_imports("jacked.service.supervisors")
        assert "jacked.service.supervisors.launchd" in found
        assert "jacked.service.supervisors.systemd" in found
        assert "jacked.service.supervisors._winproc" not in found  # a top-level import
        assert mixed_generation.lazy_imports("jacked.module_that_does_not_exist") == set()

    def test_a_root_that_does_not_import_raises(self, monkeypatch):
        monkeypatch.setattr(mixed_generation, "UPGRADE_MODULES", ("jacked.module_that_does_not_exist",))
        with pytest.raises(ModuleNotFoundError):
            mixed_generation.preload_upgrade_modules()

    def test_a_lazy_module_that_does_not_import_is_skipped_and_named(self, monkeypatch):
        real = mixed_generation.lazy_imports
        monkeypatch.setattr(
            mixed_generation,
            "lazy_imports",
            lambda name: real(name) | ({"jacked.module_that_does_not_exist"} if name == "jacked.proctree" else set()),
        )
        lines = []
        mixed_generation.preload_upgrade_modules(log=lines.append)
        assert lines == ["preload: skipped jacked.module_that_does_not_exist (ModuleNotFoundError)"]

    @pytest.mark.parametrize("context", ["jacked.service.updater", "jacked.cli"])
    def test_after_the_preload_no_lazy_import_is_left_to_read_a_new_file(self, context):
        """In a fresh interpreter in the tray context (the updater) and the CLI
        context, every lazy ``jacked`` import of every loaded module (the CLI
        module aside) is loaded after the preload, or does not import here."""
        code = f"""
import importlib, sys
import {context}
from jacked import mixed_generation as mg
mg.preload_upgrade_modules()
missing = set()
for name in [m for m in sys.modules if m.startswith("jacked") and m != "jacked.cli"]:
    for target in mg.lazy_imports(name):
        if target in sys.modules:
            continue
        try:
            importlib.util.find_spec(target)
            importlib.import_module(target)
        except Exception:
            continue
        missing.add(target)
print(sorted(missing))
"""
        out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr[-2000:]
        assert out.stdout.strip() == "[]", out.stdout

    def test_the_cli_entry_reaches_every_step_of_the_transaction(self):
        """The CLI transaction walks cli.py from ``_run_upgrade_inline``; the
        steps that run after the install must be on that graph."""
        import ast
        import jacked.cli as cli_module

        tree = ast.parse(Path(cli_module.__file__).read_text(encoding="utf-8"))
        reached = mixed_generation.reachable_functions(tree, "_run_upgrade_inline")
        for step in (
            "_run_upgrade_inline_locked", "_upgrade_install_package", "_upgrade_run_preflight",
            "_upgrade_migrate_settings", "_upgrade_restart_service", "_upgrade_rollback_and_exit",
            "_index_lag", "_restart_after_interrupt",
        ):
            assert step in reached, step
        assert mixed_generation.reachable_functions(tree, "no_such_function") == set()

    def test_after_the_cli_preload_every_lazy_import_of_its_transaction_is_loaded(self):
        code = (
            "import sys, jacked.cli\nfrom jacked import mixed_generation as mg\n"
            "mg.preload_upgrade_modules(entry=('jacked.cli', '_run_upgrade_inline'))\n"
            "print(sorted(t for t in mg.lazy_imports('jacked.cli', entry='_run_upgrade_inline') if t not in sys.modules))"
        )
        out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, out.stderr[-2000:]
        assert out.stdout.strip() == "[]", out.stdout

    def test_the_cli_transaction_preloads_before_it_takes_the_lock(self, monkeypatch):
        from jacked import cli

        order = []
        monkeypatch.setattr(
            mixed_generation, "preload_upgrade_modules", lambda **kw: order.append(("preload", kw.get("entry")))
        )

        def _lock(*args, **kwargs):
            order.append("lock")
            raise RuntimeError("stop here")

        monkeypatch.setattr(cli, "_acquire_upgrade_lock", _lock)
        with pytest.raises(RuntimeError, match="stop here"):
            cli._run_upgrade_inline(
                cmd=["false"], label="x", extras="tray", skip_service=True,
                pid_file=None, host="127.0.0.1", port=0,
            )
        assert order == [("preload", ("jacked.cli", "_run_upgrade_inline")), "lock"]

    def test_a_failed_preload_refuses_and_restarts_the_stopped_service(self, monkeypatch, tmp_path):
        from jacked import install_method
        from jacked.service import platform, update_status, updater

        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "jacked-update.log")
        monkeypatch.setattr(updater, "RECOVERY_FILE", tmp_path / "jacked-update-failed.txt")

        def _boom(**_):
            raise ImportError("cannot import name 'system_program'")

        monkeypatch.setattr(mixed_generation, "preload_upgrade_modules", _boom)
        gate_calls, marked, restarts = [], [], []
        monkeypatch.setattr(install_method, "can_auto_upgrade", lambda: gate_calls.append(1) or (False, "never reached"))
        monkeypatch.setattr(updater, "wait_for_exit", lambda pid, timeout=30.0: True)
        monkeypatch.setattr(update_status, "mark_failed", lambda path, error, recovery=None: marked.append(error))
        monkeypatch.setattr(update_status, "acquire_update_lock", lambda path: _Lock())
        monkeypatch.setattr(platform, "native_restart", lambda: restarts.append(1) or (True, "kickstarted"))
        updater.run_update(parent_pid=1, extras="tray")
        assert gate_calls == []
        log = (tmp_path / "jacked-update.log").read_text(encoding="utf-8")
        assert "REFUSED: the installed jacked cannot import its upgrade modules (ImportError)" in log
        assert "Refusal: service restart OK (kickstarted)" in log
        assert restarts == [1]
        assert marked == ["update refused: the installed jacked cannot import its upgrade modules (ImportError)"]
        recovery = (tmp_path / "jacked-update-failed.txt").read_text(encoding="utf-8")
        assert "cannot import its upgrade modules (ImportError)" in recovery
        assert "uv tool install" in recovery


class _Lock:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class TestRefuseAndRestore:
    def _run(self, monkeypatch, *, lock="free", alive=False, restart=(True, "ok"), fallback="ok",
             noop_version=None):
        from jacked.service import platform, update_status
        from jacked.service.update_refusal import refuser

        lines, recovery, marked, closed, started, noops = [], [], [], [], [], []
        handle = _Lock()

        def _acquire(path):
            if lock == "raises":
                raise OSError("read-only")
            return None if lock == "busy" else handle

        monkeypatch.setattr(update_status, "acquire_update_lock", _acquire)
        monkeypatch.setattr(update_status, "mark_failed", lambda path, error, recovery=None: marked.append(error))
        monkeypatch.setattr(
            "jacked.service.update_noop.close_preinit_as_noop",
            lambda path, installed, note: noops.append((installed, note)) or True,
        )

        def _restart():
            if isinstance(restart, Exception):
                raise restart
            return restart

        def _fallback():
            if fallback == "raises":
                raise OSError("no jacked")
            started.append(1)

        monkeypatch.setattr(platform, "native_restart", _restart)
        refuse = refuser(
            4242, lines.append, recovery.append, lambda pid, timeout: not alive,
            close=lambda: closed.append(1), start_fallback=_fallback,
        )
        if noop_version is None:
            result = refuse("a reason", "recovery text")
        else:
            result = refuse("a reason", None, noop_version=noop_version)
        return {"result": result, "lines": lines, "recovery": recovery, "marked": marked,
                "closed": closed, "started": started, "lock": handle, "noops": noops}

    def test_with_the_lock_it_closes_the_record_restarts_and_releases(self, monkeypatch):
        r = self._run(monkeypatch)
        assert r["result"] is True
        assert r["lines"] == ["REFUSED: a reason", "Refusal: service restart OK (ok)"]
        assert r["recovery"] == ["recovery text"]
        assert r["marked"] == ["update refused: a reason"]
        assert r["started"] == []
        assert r["lock"].closed == 1
        assert r["closed"] == [1]

    def test_a_no_op_writes_no_recovery_and_marks_nothing_failed(self, monkeypatch):
        """UG-1: an install that would go back is a no-op, not a failure. The
        tray's record closes as done at the installed version, and the
        service still restarts."""
        r = self._run(monkeypatch, noop_version="0.111.2")
        assert r["result"] is True
        assert r["recovery"] == [] and r["marked"] == []
        assert r["noops"] == [("0.111.2", "a reason")]
        assert r["lines"] == ["REFUSED: a reason", "Refusal: service restart OK (ok)"]
        assert r["lock"].closed == 1 and r["closed"] == [1]

    @pytest.mark.parametrize("lock", ["busy", "raises"])
    def test_without_the_lock_a_no_op_still_writes_no_recovery(self, monkeypatch, lock):
        r = self._run(monkeypatch, lock=lock, noop_version="0.111.2")
        assert r["recovery"] == [] and r["noops"] == [] and r["marked"] == []

    @pytest.mark.parametrize("lock", ["busy", "raises"])
    def test_without_the_lock_neither_the_record_nor_the_service_changes(self, monkeypatch, lock):
        r = self._run(monkeypatch, lock=lock)
        assert r["result"] is False
        assert r["marked"] == [] and r["started"] == []
        assert r["recovery"] == ["recovery text"]
        assert "left" in r["lines"][-1]
        assert r["closed"] == [1]

    def test_a_live_old_service_is_left_alone_and_the_lock_released(self, monkeypatch):
        r = self._run(monkeypatch, alive=True)
        assert r["result"] is False
        assert r["lines"][-1] == "Refusal: the old service did not exit; nothing restarted"
        assert r["started"] == []
        assert r["lock"].closed == 1

    @pytest.mark.parametrize("restart", [(False, "no supervisor artifact"), RuntimeError("no")])
    def test_a_failed_supervisor_restart_falls_back_to_a_detached_start(self, monkeypatch, restart):
        r = self._run(monkeypatch, restart=restart)
        assert r["result"] is False
        assert r["started"] == [1]
        assert r["lines"][-2].startswith("Refusal: service restart FAILED (")
        assert r["lines"][-1] == "Refusal: fallback start spawned"
        assert r["lock"].closed == 1

    def test_a_fallback_that_raises_is_logged(self, monkeypatch):
        r = self._run(monkeypatch, restart=(False, "x"), fallback="raises")
        assert r["lines"][-1] == "Refusal: fallback start raised OSError"
        assert r["closed"] == [1]


class TestWalkEdges:
    def test_a_relative_lazy_import_is_resolved(self):
        assert "jacked.codex.credentials" in mixed_generation.lazy_imports("jacked.codex.usage")

    def test_a_find_spec_that_raises_skips_the_name(self, monkeypatch):
        real = importlib.util.find_spec

        def _find(name, *args):
            if name.startswith("jacked.service.supervisors.launchd"):
                raise RuntimeError("broken parent")
            return real(name, *args)

        monkeypatch.setattr(importlib.util, "find_spec", _find)
        # The submodule probes under launchd raise; the walk goes on, and a
        # module named in `from X import` still counts without a probe.
        found = mixed_generation.lazy_imports("jacked.service.supervisors")
        assert {"jacked.service.supervisors.launchd", "jacked.service.supervisors.systemd"} <= found
        # Its own source lookup raises: no source, no lazy imports.
        assert mixed_generation.lazy_imports("jacked.service.supervisors.launchd") == set()
        assert mixed_generation._is_submodule("jacked.service.supervisors.launchd.anything") is False


class TestNamesFromMissingOnDisk:
    def test_a_name_missing_from_the_disk_too_raises_import_error(self):
        with pytest.raises(ImportError, match="cannot import name 'no_such_name' from 'jacked.winproc'"):
            mixed_generation.names_from("jacked.winproc", "no_such_name")


class TestMixedGenerationKeepsItsImportedNames:
    def test_other_modules_import_only_the_stable_names(self):
        """From 0.111.1 on, every upgrade process holds this module, and
        ``names_from`` cannot guard a name added to its own module. So the
        names other modules import from it are a fixed set: a new one must be
        added here on purpose, with a getattr fallback at its call site."""
        import ast

        imported = set()
        for path in Path(jacked.__file__).resolve().parent.rglob("*.py"):
            if path.name == "mixed_generation.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.module == "jacked.mixed_generation":
                    imported.update(a.name for a in node.names)
        assert imported <= mixed_generation.STABLE_NAMES, sorted(imported - mixed_generation.STABLE_NAMES)
        assert all(hasattr(mixed_generation, name) for name in mixed_generation.STABLE_NAMES)
