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
import sys

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


@pytest.mark.parametrize("module", ["jacked.service.supervisors", "jacked.proctree", "jacked.service.process"])
class TestNewModulesImportInAStaleInterpreter:
    """The three modules an older upgrade process first-imports after the install."""

    def test_the_module_imports_when_the_held_winproc_predates_it(self, monkeypatch, module):
        import jacked.winproc as winproc

        monkeypatch.delattr(winproc, "system_program")
        for name in [n for n in sys.modules if n == module or n.startswith(module + ".")]:
            monkeypatch.delitem(sys.modules, name)
        loaded = importlib.import_module(module)
        assert loaded.system_program is winproc.system_program
        assert callable(winproc.system_program)

    def test_a_current_interpreter_never_reloads(self, monkeypatch, module):
        calls = []
        monkeypatch.setattr(importlib, "reload", lambda m: calls.append(m.__name__))
        for name in [n for n in sys.modules if n == module or n.startswith(module + ".")]:
            monkeypatch.delitem(sys.modules, name)
        importlib.import_module(module)
        assert calls == []


class TestPreloadUpgradeModules:
    def test_every_transaction_module_is_loaded_and_named_in_order(self, monkeypatch):
        import jacked.service as service_package

        for name in mixed_generation.UPGRADE_MODULES:
            monkeypatch.delitem(sys.modules, name, raising=False)
            monkeypatch.delattr(service_package, name.rsplit(".", 1)[1], raising=False)
        monkeypatch.delattr(jacked, "proctree", raising=False)
        assert mixed_generation.preload_upgrade_modules() == mixed_generation.UPGRADE_MODULES
        for name in mixed_generation.UPGRADE_MODULES:
            assert name in sys.modules, name

    def test_the_cli_transaction_preloads_before_it_takes_the_lock(self, monkeypatch):
        from jacked import cli

        order = []
        monkeypatch.setattr(mixed_generation, "preload_upgrade_modules", lambda: order.append("preload"))

        def _lock(*args, **kwargs):
            order.append("lock")
            raise RuntimeError("stop here")

        monkeypatch.setattr(cli, "_acquire_upgrade_lock", _lock)
        with pytest.raises(RuntimeError, match="stop here"):
            cli._run_upgrade_inline(
                cmd=["false"], label="x", extras="tray", skip_service=True,
                pid_file=None, host="127.0.0.1", port=0,
            )
        assert order == ["preload", "lock"]

    def test_the_tray_transaction_refuses_before_any_write_when_the_preload_fails(self, monkeypatch, tmp_path):
        from jacked import install_method
        from jacked.service import updater

        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "jacked-update.log")
        monkeypatch.setattr(updater, "RECOVERY_FILE", tmp_path / "jacked-update-failed.txt")

        def _boom():
            raise ImportError("cannot import name 'system_program'")

        monkeypatch.setattr(mixed_generation, "preload_upgrade_modules", _boom)
        gate_calls = []
        monkeypatch.setattr(install_method, "can_auto_upgrade", lambda: gate_calls.append(1) or (False, "never reached"))
        updater.run_update(parent_pid=1, extras="tray")
        assert gate_calls == []
        assert "REFUSED: the installed jacked cannot import its upgrade modules (ImportError)" in (
            tmp_path / "jacked-update.log").read_text(encoding="utf-8")
        recovery = (tmp_path / "jacked-update-failed.txt").read_text(encoding="utf-8")
        assert "cannot import its upgrade modules (ImportError)" in recovery
        assert 'uv tool install "claude-jacked[tray]" --force --refresh' in recovery
