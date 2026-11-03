"""Route handlers write files only through the shared atomic writer.

Many handlers run in the threadpool (no synchronous jacked.db work on the
event loop, #162i), so two requests can write the same file at once. A plain
``write_text`` truncates first and writes second, so two writers interleave
into a file that is neither version. ``jacked.memory.vault.atomic_write_text``
(``jacked.atomic_io``) writes a writer-unique temp file and replaces the
target in one step, retrying a Windows sharing refusal.

That retry pauses (up to 0.9 s by default), so no coroutine may call the
writer on the event loop: one save would stop every other request. The
guard at the bottom proves it from the source, and the route tests prove it
at run time, with the lock that serializes each read-modify-write held
across the worker hop.
"""

import ast
import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests._platform import requires_symlinks
from tests.unit.api import test_db_off_loop_guard as db_guard

ROUTES = Path(__file__).resolve().parents[3] / "jacked" / "api" / "routes"


def _raw_writes(path: Path) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in {"write_text", "write_bytes"}:
            found.append(f"{path.name}:{node.lineno} .{func.attr}()")
        is_open = (isinstance(func, ast.Name) and func.id == "open") or (
            isinstance(func, ast.Attribute) and func.attr == "open"
        )
        if not is_open:
            continue
        # open(path, "w") puts the mode second; Path.open("w") puts it first.
        position = 1 if isinstance(func, ast.Name) else 0
        mode = next((kw.value for kw in node.keywords if kw.arg == "mode"), None)
        if mode is None and len(node.args) > position:
            mode = node.args[position]
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str) and set(mode.value) & set("wax+"):
            found.append(f"{path.name}:{node.lineno} open(..., {mode.value!r})")
    return found


def test_route_modules_have_no_raw_file_writes():
    offenders = [hit for path in sorted(ROUTES.glob("*.py")) for hit in _raw_writes(path)]
    assert not offenders, "use jacked.memory.vault.atomic_write_text: " + ", ".join(offenders)


def test_the_guard_sees_a_raw_write(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "from pathlib import Path\nPath('x').write_text('y')\nopen('z', 'w')\n"
        "open('z', mode='a')\nPath('z').open('w')\nPath('z').open(mode='x')\n"
        "open('r')\nPath('z').open()\n"
    )
    assert len(_raw_writes(bad)) == 5


def test_concurrent_lesson_saves_never_mix_their_contents(tmp_path):
    """Two saves of different lengths race; both return and the file ends as
    one of them. A thread's exception is only a pytest warning, so each one is
    collected and asserted: on Windows a replace refused while the other
    writer holds the file (WinError 5) must be retried, not lost (#237i)."""
    import threading

    from jacked.memory.vault import atomic_write_text

    target = tmp_path / "lessons.md"
    long_text, short_text = "L" * 200_000 + "\n", "S\n"
    barrier = threading.Barrier(2)
    errors: list = []

    def save(text):
        barrier.wait()
        try:
            atomic_write_text(target, text)
        except BaseException as exc:  # collected: a thread cannot fail the test
            errors.append(exc)

    for _ in range(25):
        threads = [threading.Thread(target=save, args=(t,)) for t in (long_text, short_text)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors
        assert target.read_text() in (long_text, short_text)


def _lessons_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


def _lessons_routes(repo):
    from jacked.api.routes import system

    body = system.LessonsUpdateRequest(
        repo_path=str(repo), lessons=[system.LessonItem(index=0, strike=1, text="Use uv")]
    )
    return {
        "get": lambda: system.get_project_lessons(str(repo), _request()),
        "put": lambda: system.update_project_lessons(body, _request()),
    }


@requires_symlinks
@pytest.mark.parametrize("route", ["get", "put"])
def test_a_lessons_file_linked_outside_the_project_is_refused(tmp_path, route):
    """A repository can ship lessons.md as a link to a private file. The save
    used to copy that file's text (its header lines) into the project's
    lessons.md, and the read showed its bullet lines in the dashboard."""
    repo = _lessons_repo(tmp_path)
    private = tmp_path / "credentials"
    private.write_text("[default]\naws_secret_access_key = secret\n- a bullet\n")
    (repo / "lessons.md").symlink_to(private)

    result = _lessons_routes(repo)[route]()

    assert result.status_code == 422
    error = json.loads(result.body)["error"]
    assert error["code"] == "LINK_OUTSIDE_PROJECT"
    assert error["message"].startswith("The lessons.md path in this project is a link")
    assert (repo / "lessons.md").is_symlink()  # no copy of the private file
    assert private.read_text() == "[default]\naws_secret_access_key = secret\n- a bullet\n"


@requires_symlinks
def test_a_lessons_link_made_after_the_check_is_refused_before_the_write(
    tmp_path, monkeypatch
):
    """The handler checks the link, reads the header lines, then writes. A
    link made after the check (another local process racing the dashboard)
    is caught by the second check, right before the write."""
    from jacked.api.routes import system

    repo = _lessons_repo(tmp_path)
    private = tmp_path / "credentials"
    private.write_text("[default]\naws_secret_access_key = secret\n")
    real_check = system.first_outside_project
    checks = []

    def late_link(project, *relative):
        checks.append(relative)
        if len(checks) == 1:
            # The first check passes; the link appears right after it.
            (repo / "lessons.md").symlink_to(private)
            return None
        return real_check(project, *relative)

    monkeypatch.setattr(system, "first_outside_project", late_link)

    result = _lessons_routes(repo)["put"]()

    assert result.status_code == 422
    assert json.loads(result.body)["error"]["code"] == "LINK_OUTSIDE_PROJECT"
    assert checks == [("lessons.md",), ("lessons.md",)]
    assert (repo / "lessons.md").is_symlink()  # not replaced by a copy
    assert private.read_text() == "[default]\naws_secret_access_key = secret\n"


def test_a_normal_lessons_file_is_saved_and_read(tmp_path):
    repo = _lessons_repo(tmp_path)
    routes = _lessons_routes(repo)

    assert routes["put"]() == {"saved": True, "count": 1}
    assert routes["get"]() == {
        "exists": True,
        "lessons": [{"index": 2, "strike": 1, "text": "Use uv"}],
    }


def test_the_atomic_writer_follows_a_symlink_only_when_asked(tmp_path):
    """Default: a link is replaced by a plain file, so a repo-shipped
    lessons.md that links to ~/.bashrc cannot redirect a save. The user's own
    global CLAUDE.md opts in and stays a link to its dotfiles copy."""
    import os

    import pytest

    from jacked.memory.vault import atomic_write_text

    outside = tmp_path / "home" / ".bashrc"
    outside.parent.mkdir()
    outside.write_text("export SAFE=1\n")
    repo_lessons = tmp_path / "repo" / "lessons.md"
    repo_lessons.parent.mkdir()
    try:
        repo_lessons.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    atomic_write_text(repo_lessons, "# Lessons\n")
    assert not os.path.islink(repo_lessons) and repo_lessons.read_text() == "# Lessons\n"
    assert outside.read_text() == "export SAFE=1\n"

    shared = tmp_path / "dotfiles" / "CLAUDE.md"
    shared.parent.mkdir()
    shared.write_text("old\n")
    link = tmp_path / "CLAUDE.md"
    link.symlink_to(shared)
    atomic_write_text(link, "new\n", follow_symlinks=True)
    assert os.path.islink(link) and shared.read_text() == "new\n"


def test_only_the_global_claude_md_follows_links():
    """Every route write is no-follow except the ~/.claude/CLAUDE.md toggles."""
    following = []
    for path in sorted(ROUTES.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "atomic_write_text":
                if any(kw.arg == "follow_symlinks" for kw in node.keywords):
                    following.append(f"{path.name}:{ast.unparse(node.args[0])}")
    assert following and all(hit.endswith(":CLAUDE_MD") for hit in following), following


# --- the writer runs off the event loop: the source guard ---------------------

# The shared writer's entry points. A sync function of the package that calls
# one of them, at any depth, can pause like the writer itself.
WRITERS = frozenset({
    "jacked.atomic_io.atomic_write_bytes",
    "jacked.atomic_io.atomic_write_text",
    "jacked.atomic_io.replace_with_retry",
})


def _canonical(target):
    """Follow a re-export to the function it names:
    ``jacked.memory.vault.atomic_write_text`` is the atomic_io writer."""
    seen = set()
    while target and target not in seen:
        seen.add(target)
        module, _, name = target.rpartition(".")
        if module not in db_guard._modules() or target in db_guard._sync_functions():
            return target
        names, _ = db_guard._imports(module)
        if name not in names:
            return target
        target = names[name]
    return target


def _writes_on_the_loop() -> list[str]:
    """Coroutines that call the writer, directly or through sync functions.

    This reuses the jacked.db guard's source machinery (module table, import
    resolution, own-call walk). Like that guard, it sees calls by name: a
    method called on an object (``store.write()``) needs the run-time tests.
    """
    modules = db_guard._modules()
    parents = {module: db_guard._parents(tree) for module, tree in modules.items()}

    def target_of(call, module):
        return _canonical(db_guard._resolve(call, module))

    pausing = set(WRITERS)
    changed = True
    while changed:
        changed = False
        for name, (module, node) in db_guard._sync_functions().items():
            if name not in pausing and any(
                target_of(call, module) in pausing
                for call in db_guard._own_calls(node, parents[module])
            ):
                pausing.add(name)
                changed = True
    found = []
    for module, tree in modules.items():
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            for call in db_guard._own_calls(fn, parents[module]):
                target = target_of(call, module)
                if target in pausing:
                    found.append(f"{module}:{call.lineno} {fn.name}() calls {target}")
    return sorted(set(found))


def _clear_guard_caches():
    for cached in (
        db_guard._sync_functions,
        db_guard._blocking_sync_functions,
        db_guard._imports,
        db_guard._aliases,
    ):
        cached.cache_clear()


def test_no_coroutine_runs_the_atomic_writer_on_the_event_loop():
    found = _writes_on_the_loop()
    assert not found, (
        "Send these writes to a thread (`await asyncio.to_thread(...)`) and "
        "keep the lock that serializes the read-modify-write held across the "
        "await:\n" + "\n".join(found)
    )


def test_the_writer_guard_sees_every_call_form(monkeypatch):
    """The detector itself: a direct call, a call through a sync helper, a
    call through a module alias, a re-exported writer, and the allowed
    ``asyncio.to_thread`` forms."""
    src = (
        "import asyncio\n"
        "from jacked.memory.vault import atomic_write_text\n"
        "from jacked import atomic_io\n"
        "def save(path):\n    atomic_write_text(path, 'x')\n"
        "async def direct(path):\n    atomic_write_text(path, 'x')\n"
        "async def through_helper(path):\n    save(path)\n"
        "async def through_module(path):\n    atomic_io.replace_with_retry(path, path)\n"
        "async def allowed(path):\n"
        "    await asyncio.to_thread(save, path)\n"
        "    await asyncio.to_thread(atomic_write_text, path, 'x')\n"
    )
    module = "jacked._writer_guard_probe"
    _clear_guard_caches()
    db_guard._modules.cache_clear()
    probed = {**db_guard._modules(), module: ast.parse(src)}
    monkeypatch.setattr(db_guard, "_modules", lambda: probed)
    try:
        _clear_guard_caches()
        probe = [hit for hit in _writes_on_the_loop() if hit.startswith(module)]
    finally:
        _clear_guard_caches()
    assert probe == [
        f"{module}:11 through_module() calls jacked.atomic_io.replace_with_retry",
        f"{module}:7 direct() calls jacked.atomic_io.atomic_write_text",
        f"{module}:9 through_helper() calls {module}.save",
    ]


# --- the writer runs off the event loop: the routes at run time ---------------


def _request(db=None):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=db)))


def _run_on_loop(make_coro):
    """Run ``make_coro()`` under asyncio.run; return (result, loop thread id)."""

    async def main():
        return await make_coro(), threading.get_ident()

    return asyncio.run(main())


def _settings_routes():
    from jacked.api.routes import features as f
    from jacked.api.routes import permissions as p

    return {
        "raw": lambda: f.set_raw_settings(
            f.RawSettingsRequest(content={"a": 1}, confirm_overwrite=True)
        ),
        "env": lambda: f.set_claude_env(
            "DISABLE_PROMPT_CACHING", f.EnvToggleRequest(enabled=True)
        ),
        "key": lambda: f.set_claude_key(
            "spinnerTipsEnabled", f.DirectSettingRequest(value=True)
        ),
        "plugin": lambda: f.toggle_claude_plugin(
            "probe@market", f.PluginToggleRequest(enabled=False)
        ),
        "permissions": lambda: f.set_claude_permissions(
            f.PermissionsRequest(defaultMode="plan")
        ),
        "sounds-hook": lambda: f._toggle_hook("sounds", False),
        "global-rule": lambda: p.add_permission_rule(
            p.AddRuleRequest(pattern="Bash(git status:*)"), _request()
        ),
    }


@pytest.mark.parametrize("route", sorted(_settings_routes()))
def test_settings_json_is_written_off_the_loop_under_the_settings_lock(
    route, tmp_path, monkeypatch
):
    from jacked.api.routes import features

    monkeypatch.setattr(features, "SETTINGS_JSON", tmp_path / "settings.json")
    writes: list = []

    def write(path, text, **kwargs):
        writes.append((threading.get_ident(), features._settings_lock.locked()))

    monkeypatch.setattr(features, "atomic_write_text", write)

    result, loop_thread = _run_on_loop(_settings_routes()[route])

    assert not hasattr(result, "status_code"), result.body
    assert len(writes) == 1
    [(thread, locked)] = writes
    assert thread != loop_thread
    assert locked  # the read-modify-write stays serialized across the hop


@pytest.mark.parametrize("route", ["project-permissions", "project-rule"])
def test_project_settings_are_written_off_the_loop_under_their_lock(
    route, tmp_path, monkeypatch
):
    from jacked.api.routes import permissions

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    writes: list = []

    def write(path, text, **kwargs):
        writes.append((threading.get_ident(), permissions._project_settings_lock.locked()))

    monkeypatch.setattr(permissions, "atomic_write_text", write)
    make = {
        "project-permissions": lambda: permissions.set_project_permissions(
            permissions.ProjectPermissionsRequest(repo_path=str(repo), defaultMode="plan"),
            _request(),
        ),
        "project-rule": lambda: permissions.add_permission_rule(
            permissions.AddRuleRequest(
                pattern="Bash(git status:*)", scope="project", repo_path=str(repo)
            ),
            _request(),
        ),
    }[route]

    result, loop_thread = _run_on_loop(make)

    assert not hasattr(result, "status_code"), result.body
    assert len(writes) == 1
    [(thread, locked)] = writes
    assert thread != loop_thread
    assert locked


@pytest.mark.parametrize("enabled", [True, False], ids=["enable", "disable"])
def test_the_rules_toggle_writes_claude_md_off_the_loop_under_its_lock(
    enabled, tmp_path, monkeypatch
):
    from jacked.api.routes import features

    claude_md = tmp_path / "CLAUDE.md"
    rules = (features.DATA_ROOT / "rules" / "jacked_behaviors.md").read_text(encoding="utf-8")
    claude_md.write_text("# Mine\n\n" + (rules.strip() + "\n" if not enabled else ""), encoding="utf-8")
    monkeypatch.setattr(features, "CLAUDE_MD", claude_md)
    writes: list = []

    def write(path, text, **kwargs):
        lock = getattr(features, "_rules_lock", None)
        writes.append((threading.get_ident(), lock is not None and lock.locked()))
        assert kwargs == {"follow_symlinks": True}

    monkeypatch.setattr(features, "atomic_write_text", write)

    result, loop_thread = _run_on_loop(lambda: features._toggle_rules(enabled))

    assert result == {"name": "rules", "category": "knowledge", "enabled": enabled}
    assert len(writes) == 1
    [(thread, locked)] = writes
    assert thread != loop_thread
    assert locked  # two toggles cannot interleave their read and write


def test_two_rules_toggles_never_interleave(tmp_path, monkeypatch):
    """Enable then disable at once: the disable must see the enabled file.
    Without the lock it reads the old file, reports success, and the
    enable's late write leaves the rules installed."""
    from jacked.api.routes import features

    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# Mine\n", encoding="utf-8")
    monkeypatch.setattr(features, "CLAUDE_MD", claude_md)
    real_write = features.atomic_write_text
    release = threading.Event()

    def slow_write(path, text, **kwargs):
        # The first write (the enable) waits in its worker thread, so the
        # disable runs while the enable has read but not yet written.
        if "jacked-behaviors" in text:
            release.wait(timeout=5)
        real_write(path, text, **kwargs)

    monkeypatch.setattr(features, "atomic_write_text", slow_write)

    async def both():
        enable = asyncio.create_task(features._toggle_rules(True))
        await asyncio.sleep(0.05)
        disable = asyncio.create_task(features._toggle_rules(False))
        # Serialized, the disable waits for the enable and cannot finish
        # here. Unserialized, it finishes at once against the old file.
        await asyncio.wait({disable}, timeout=0.5)
        release.set()
        return await enable, await disable

    enabled, disabled = asyncio.run(both())

    assert enabled["enabled"] is True and disabled["enabled"] is False
    assert features.RULES_START_PREFIX not in claude_md.read_text(encoding="utf-8")
