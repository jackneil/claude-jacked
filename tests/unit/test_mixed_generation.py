"""An interpreter mid-upgrade keeps importing the new release's modules.

`jacked upgrade` replaces the package on disk under a running process. The
0.110.2 upgrade to 0.111.0 died at its restart step because the old
interpreter imported the new `jacked.service.supervisors` package, which
needs `system_program`, a name 0.111.0 added to `jacked.winproc`, while the
process still held the 0.110.2 module. These tests pin both halves of the
fix: the guard that reloads a held module missing a name, and the upgrade
command that imports its modules before the install.
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


class TestSupervisorsImportInAStaleInterpreter:
    def test_the_package_imports_when_the_held_winproc_predates_it(self, monkeypatch):
        """The exact failure of the 0.110.2 upgrade to 0.111.0, in one process."""
        import jacked.winproc as winproc

        monkeypatch.delattr(winproc, "system_program")
        for name in [n for n in sys.modules if n.startswith("jacked.service.supervisors")]:
            monkeypatch.delitem(sys.modules, name)
        package = importlib.import_module("jacked.service.supervisors")
        assert package.system_program is winproc.system_program
        assert callable(winproc.system_program)

    def test_a_current_interpreter_never_reloads(self, monkeypatch):
        calls = []
        monkeypatch.setattr(importlib, "reload", lambda module: calls.append(module.__name__))
        for name in [n for n in sys.modules if n.startswith("jacked.service.supervisors")]:
            monkeypatch.delitem(sys.modules, name)
        importlib.import_module("jacked.service.supervisors")
        assert calls == []


class TestUpgradePreloadsItsModules:
    MODULES = ("jacked.service.legacy", "jacked.service.lifecycle", "jacked.service.updater")

    def test_the_transaction_modules_are_loaded_before_the_install(self, monkeypatch):
        import jacked.service as service_package
        from jacked import cli

        for name in self.MODULES:
            monkeypatch.delitem(sys.modules, name, raising=False)
            monkeypatch.delattr(service_package, name.rsplit(".", 1)[1], raising=False)
        cli._preload_upgrade_modules()
        for name in self.MODULES:
            assert name in sys.modules, name

    def test_the_inline_upgrade_preloads_before_it_takes_the_lock(self, monkeypatch):
        """The preload runs first; the lock, the install and the rest never start here."""
        from jacked import cli

        order = []
        monkeypatch.setattr(cli, "_preload_upgrade_modules", lambda: order.append("preload"))

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
