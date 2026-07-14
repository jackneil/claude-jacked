"""Tests for the skill-pack dashboard routes — GET/PUT /api/packs.

Every ``jacked.packs`` boundary the routes touch is monkeypatched so the
tests never spawn ``npx``, hit the network, or write to the real home.
The route module accesses those functions as module attributes
(``packs.load_registry`` etc.), so patching ``jacked.packs.*`` is seen at
call time.

The only intentional exception is the npx-missing case, which leaves the
real ``install_pack`` in place: with ``find_npx`` patched to ``None`` it
early-returns the Node install message before running any subprocess, so
it exercises the real branch while staying hermetic.
"""

from fastapi import FastAPI
from starlette.testclient import TestClient

import jacked.packs as packs
from jacked.api.routes.packs import router


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return app


def _sample_registry() -> dict[str, packs.Pack]:
    # Both are opt-in (default=False) so the enable/disable operational tests
    # exercise a normal toggle; default-on behavior gets its own registry in
    # test_get_default_pack_reads_enabled_without_explicit_state.
    return {
        "marketing": packs.Pack(
            name="marketing",
            display_name="Marketing Skills",
            description="Marketing bundle",
            source="coreyhaines31/marketingskills",
            homepage="https://github.com/coreyhaines31/marketingskills",
            skills=("ads", "seo"),
            default=False,
        ),
        "design-extras": packs.Pack(
            name="design-extras",
            display_name="Design Extras",
            description="Design bundle",
            source="emilkowalski/skills",
            homepage="https://github.com/emilkowalski/skills",
            skills=("improve-animations",),
            default=False,
        ),
    }


def _fake_pack_status(pack: packs.Pack, home) -> dict:
    """Deterministic stand-in for packs.pack_status (no disk reads)."""
    return {
        "name": pack.name,
        "display_name": pack.display_name,
        "description": pack.description,
        "homepage": pack.homepage,
        "source": pack.source,
        "skills": [
            {"name": s, "installed": False, "source_ok": None, "updated_at": None}
            for s in pack.skills
        ],
        "installed_count": 0,
        "total": len(pack.skills),
    }


def test_get_packs_shape(monkeypatch):
    """GET returns npx_available plus every registry pack (sorted by name), each
    carrying the effective ``enabled`` flag (is_effectively_enabled), the
    registry ``default`` flag, and ``explicit`` (whether a state decision was
    recorded, i.e. pack_state is not None)."""
    monkeypatch.setattr(packs, "load_registry", lambda data_root: _sample_registry())
    monkeypatch.setattr(
        packs, "is_effectively_enabled", lambda pack, home: pack.name == "marketing"
    )
    monkeypatch.setattr(
        packs, "pack_state", lambda home, name: "enabled" if name == "marketing" else None
    )
    monkeypatch.setattr(packs, "pack_status", _fake_pack_status)
    monkeypatch.setattr(packs, "find_npx", lambda: "/usr/bin/npx")

    client = TestClient(_make_app())
    resp = client.get("/api/packs")
    assert resp.status_code == 200
    body = resp.json()

    assert body["npx_available"] is True
    names = [p["name"] for p in body["packs"]]
    assert names == ["design-extras", "marketing"]  # sorted by name

    by_name = {p["name"]: p for p in body["packs"]}
    # enabled reflects EFFECTIVE state (is_effectively_enabled), not the raw default.
    assert by_name["marketing"]["enabled"] is True
    assert by_name["design-extras"]["enabled"] is False
    # default is carried straight from the Pack.
    assert by_name["marketing"]["default"] is False
    assert by_name["design-extras"]["default"] is False
    # explicit = an explicit enabled/disabled decision was recorded (pack_state).
    assert by_name["marketing"]["explicit"] is True
    assert by_name["design-extras"]["explicit"] is False
    # pack_status fields are carried through.
    assert by_name["marketing"]["total"] == 2
    assert by_name["marketing"]["installed_count"] == 0


def test_get_packs_npx_unavailable(monkeypatch):
    """npx_available is False when find_npx returns None; with nothing enabled,
    every pack reads enabled=False."""
    monkeypatch.setattr(packs, "load_registry", lambda data_root: _sample_registry())
    monkeypatch.setattr(packs, "is_effectively_enabled", lambda pack, home: False)
    monkeypatch.setattr(packs, "pack_state", lambda home, name: None)
    monkeypatch.setattr(packs, "pack_status", _fake_pack_status)
    monkeypatch.setattr(packs, "find_npx", lambda: None)

    client = TestClient(_make_app())
    body = client.get("/api/packs").json()
    assert body["npx_available"] is False
    assert all(p["enabled"] is False for p in body["packs"])


def test_get_default_pack_reads_enabled_without_explicit_state(monkeypatch, tmp_path):
    """A default=True pack with NO recorded state reads enabled=True (the
    registry default carries), while a default=False pack with no state reads
    enabled=False. Both are explicit=False because neither was toggled.

    This runs the REAL is_effectively_enabled / pack_state against an empty
    $JACKED_HOME (no state file on disk), so the default-resolution wiring is
    exercised end to end rather than through a fake."""
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))  # empty tree -> no state file

    def _registry() -> dict[str, packs.Pack]:
        return {
            "on-by-default": packs.Pack(
                name="on-by-default",
                display_name="On By Default",
                description="Ships enabled",
                source="owner/on",
                homepage="https://github.com/owner/on",
                skills=("a",),
                default=True,
            ),
            "opt-in": packs.Pack(
                name="opt-in",
                display_name="Opt In",
                description="Ships disabled",
                source="owner/off",
                homepage="https://github.com/owner/off",
                skills=("b",),
                default=False,
            ),
        }

    monkeypatch.setattr(packs, "load_registry", lambda data_root: _registry())
    monkeypatch.setattr(packs, "pack_status", _fake_pack_status)
    monkeypatch.setattr(packs, "find_npx", lambda: "/usr/bin/npx")

    client = TestClient(_make_app())
    body = client.get("/api/packs").json()
    by_name = {p["name"]: p for p in body["packs"]}

    assert by_name["on-by-default"]["enabled"] is True
    assert by_name["on-by-default"]["default"] is True
    assert by_name["on-by-default"]["explicit"] is False

    assert by_name["opt-in"]["enabled"] is False
    assert by_name["opt-in"]["default"] is False
    assert by_name["opt-in"]["explicit"] is False


def test_put_unknown_pack_is_422(monkeypatch):
    """A name absent from the registry returns 422 in the features error shape."""
    monkeypatch.setattr(packs, "load_registry", lambda data_root: _sample_registry())

    client = TestClient(_make_app())
    resp = client.put("/api/packs/does-not-exist", json={"enabled": True})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert "Unknown pack" in err["message"]
    assert err["code"] == "INVALID_PACK"


def test_put_enable_happy_path(monkeypatch):
    """Enable records intent (set_enabled True) THEN installs, and returns the
    op result plus a fresh pack_status with enabled=True."""
    order: list = []

    def fake_set_enabled(home, name, enabled):
        order.append(("set_enabled", name, enabled))

    def fake_install(pack, home, *, include_codex, timeout=600):
        order.append(("install", pack.name))
        return packs.PackOpResult(
            ok=True,
            installed=["ads", "seo"],
            message="Installed 2 skill(s) for pack 'Marketing Skills'.",
        )

    monkeypatch.setattr(packs, "load_registry", lambda data_root: _sample_registry())
    monkeypatch.setattr(packs, "set_enabled", fake_set_enabled)
    monkeypatch.setattr(packs, "install_pack", fake_install)
    monkeypatch.setattr(packs, "pack_status", _fake_pack_status)
    monkeypatch.setattr(packs, "find_npx", lambda: "/usr/bin/npx")

    client = TestClient(_make_app())
    resp = client.put("/api/packs/marketing", json={"enabled": True})
    assert resp.status_code == 200
    body = resp.json()

    assert body["ok"] is True
    assert body["installed"] == ["ads", "seo"]
    assert "Installed 2 skill(s)" in body["message"]
    assert body["pack"]["name"] == "marketing"
    assert body["pack"]["enabled"] is True

    # set_enabled(True) is recorded before install runs.
    assert [step[0] for step in order] == ["set_enabled", "install"]
    assert order[0] == ("set_enabled", "marketing", True)


def test_put_enable_install_failure_returns_200_ok_false(monkeypatch):
    """An install that verifies short still returns HTTP 200; ok=false and the
    diagnostic message are carried in the body, and enabled stays true (intent
    wins)."""
    def fake_install(pack, home, *, include_codex, timeout=600):
        return packs.PackOpResult(
            ok=False,
            installed=["ads"],
            missing=["seo"],
            message="Installed 1/2 skills. Missing after install: seo.",
        )

    monkeypatch.setattr(packs, "load_registry", lambda data_root: _sample_registry())
    monkeypatch.setattr(packs, "set_enabled", lambda home, name, enabled: None)
    monkeypatch.setattr(packs, "install_pack", fake_install)
    monkeypatch.setattr(packs, "pack_status", _fake_pack_status)
    monkeypatch.setattr(packs, "find_npx", lambda: "/usr/bin/npx")

    client = TestClient(_make_app())
    resp = client.put("/api/packs/marketing", json={"enabled": True})
    assert resp.status_code == 200
    body = resp.json()

    assert body["ok"] is False
    assert body["installed"] == ["ads"]
    assert body["missing"] == ["seo"]
    assert "Missing after install" in body["message"]
    assert body["pack"]["enabled"] is True  # intent wins despite failure


def test_put_disable_sets_disabled_then_removes_even_when_remove_fails(monkeypatch):
    """Disable records set_enabled(False) FIRST (durable intent, survives a
    crash mid-remove), THEN runs remove_pack, and still reports the remove
    failure in the body."""
    order: list = []

    def fake_remove(pack, home, timeout=300):
        order.append(("remove", pack.name))
        return packs.PackOpResult(
            ok=False,
            removed=[],
            message="These still exist on disk after removal: seo.",
        )

    def fake_set_enabled(home, name, enabled):
        order.append(("set_enabled", name, enabled))

    monkeypatch.setattr(packs, "load_registry", lambda data_root: _sample_registry())
    monkeypatch.setattr(packs, "remove_pack", fake_remove)
    monkeypatch.setattr(packs, "set_enabled", fake_set_enabled)
    monkeypatch.setattr(packs, "pack_status", _fake_pack_status)

    client = TestClient(_make_app())
    resp = client.put("/api/packs/marketing", json={"enabled": False})
    assert resp.status_code == 200
    body = resp.json()

    assert body["ok"] is False
    assert "still exist on disk" in body["message"]
    assert body["pack"]["enabled"] is False

    # disabled intent recorded FIRST, then remove runs — regardless of result.
    assert order == [("set_enabled", "marketing", False), ("remove", "marketing")]


def test_put_disable_default_pack_is_durable(monkeypatch, tmp_path):
    """Disabling a default=True pack writes a DURABLE 'disabled' state (not a
    dropped entry), so a later GET reads enabled=False, explicit=True — the pack
    stays off across restarts instead of silently reverting to its default-on.

    Uses the REAL set_enabled/load_state/save_state/pack_state so the durability
    round-trips through disk; only the npx-touching remove_pack and lockfile
    pack_status are faked. set_enabled(False) is recorded and asserted to fire
    BEFORE remove_pack (durable intent must survive a crash mid-remove)."""
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))

    def _registry() -> dict[str, packs.Pack]:
        return {
            "on-by-default": packs.Pack(
                name="on-by-default",
                display_name="On By Default",
                description="Ships enabled",
                source="owner/on",
                homepage="https://github.com/owner/on",
                skills=("a",),
                default=True,
            ),
        }

    order: list = []
    real_set_enabled = packs.set_enabled  # captured before we shadow it below

    def spy_set_enabled(home, name, enabled):
        order.append(("set_enabled", name, enabled))
        real_set_enabled(home, name, enabled)  # persist real durable state

    def fake_remove(pack, home, timeout=300):
        order.append(("remove", pack.name))
        return packs.PackOpResult(ok=True, removed=list(pack.skills), message="Removed.")

    monkeypatch.setattr(packs, "load_registry", lambda data_root: _registry())
    monkeypatch.setattr(packs, "set_enabled", spy_set_enabled)
    monkeypatch.setattr(packs, "remove_pack", fake_remove)
    monkeypatch.setattr(packs, "pack_status", _fake_pack_status)
    monkeypatch.setattr(packs, "find_npx", lambda: "/usr/bin/npx")

    client = TestClient(_make_app())
    resp = client.put("/api/packs/on-by-default", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["pack"]["enabled"] is False

    # durable intent recorded FIRST, then remove.
    assert order == [
        ("set_enabled", "on-by-default", False),
        ("remove", "on-by-default"),
    ]

    # A subsequent GET reads the durable disabled state, NOT the default-on:
    # is_effectively_enabled/pack_state run for real against the written file.
    body = client.get("/api/packs").json()
    pack = next(p for p in body["packs"] if p["name"] == "on-by-default")
    assert pack["enabled"] is False
    assert pack["explicit"] is True


def test_put_enable_npx_missing_surfaces_node_message(monkeypatch):
    """With npx absent, the real install_pack early-returns the Node install
    message (no subprocess). The endpoint surfaces it as 200 ok=false and
    still records enable intent."""
    monkeypatch.setattr(packs, "load_registry", lambda data_root: _sample_registry())
    monkeypatch.setattr(packs, "set_enabled", lambda home, name, enabled: None)
    monkeypatch.setattr(packs, "pack_status", _fake_pack_status)
    monkeypatch.setattr(packs, "find_npx", lambda: None)  # real install_pack sees this

    client = TestClient(_make_app())
    resp = client.put("/api/packs/marketing", json={"enabled": True})
    assert resp.status_code == 200
    body = resp.json()

    assert body["ok"] is False
    assert "Node.js" in body["message"]
    assert body["missing"] == ["ads", "seo"]  # every skill reported missing
    assert body["pack"]["enabled"] is True


def test_jacked_home_env_honored_on_get(monkeypatch, tmp_path):
    """GET resolves home from $JACKED_HOME per request (not the module-level
    Path.home() from the pre-fix code), so state + status reads target that
    tree — matching the CLI's _jacked_home. The home handed to the faked packs
    functions must be exactly the env-set path."""
    fake_home = tmp_path / "alt-home"
    monkeypatch.setenv("JACKED_HOME", str(fake_home))

    seen: dict = {}

    def fake_is_effectively_enabled(pack, home):
        seen["effective_home"] = home
        return False

    def fake_pack_state(home, name):
        seen["state_home"] = home
        return None

    def fake_pack_status(pack, home):
        seen["status_home"] = home
        return _fake_pack_status(pack, home)

    monkeypatch.setattr(packs, "load_registry", lambda data_root: _sample_registry())
    monkeypatch.setattr(packs, "is_effectively_enabled", fake_is_effectively_enabled)
    monkeypatch.setattr(packs, "pack_state", fake_pack_state)
    monkeypatch.setattr(packs, "pack_status", fake_pack_status)
    monkeypatch.setattr(packs, "find_npx", lambda: "/usr/bin/npx")

    client = TestClient(_make_app())
    resp = client.get("/api/packs")
    assert resp.status_code == 200

    from pathlib import Path

    # The GET no longer calls enabled_pack_names; home now flows through the
    # effective-state resolvers and pack_status. All must see the $JACKED_HOME path.
    assert seen["effective_home"] == Path(str(fake_home))
    assert seen["state_home"] == Path(str(fake_home))
    assert seen["status_home"] == Path(str(fake_home))


def test_jacked_home_env_honored_on_put_enable(monkeypatch, tmp_path):
    """PUT enable threads the $JACKED_HOME-resolved home through set_enabled,
    install_pack (via _install_in_thread), and pack_status — all under the env
    path, so the dashboard writes state where the CLI reads it."""
    fake_home = tmp_path / "alt-home"
    monkeypatch.setenv("JACKED_HOME", str(fake_home))

    seen: dict = {}

    def fake_set_enabled(home, name, enabled):
        seen["set_enabled_home"] = home

    def fake_install(pack, home, *, include_codex, timeout=600):
        seen["install_home"] = home
        return packs.PackOpResult(
            ok=True,
            installed=list(pack.skills),
            message="Installed.",
        )

    def fake_pack_status(pack, home):
        seen["status_home"] = home
        return _fake_pack_status(pack, home)

    monkeypatch.setattr(packs, "load_registry", lambda data_root: _sample_registry())
    monkeypatch.setattr(packs, "set_enabled", fake_set_enabled)
    monkeypatch.setattr(packs, "install_pack", fake_install)
    monkeypatch.setattr(packs, "pack_status", fake_pack_status)
    monkeypatch.setattr(packs, "find_npx", lambda: "/usr/bin/npx")

    client = TestClient(_make_app())
    resp = client.put("/api/packs/marketing", json={"enabled": True})
    assert resp.status_code == 200

    from pathlib import Path

    expected = Path(str(fake_home))
    assert seen["set_enabled_home"] == expected
    assert seen["install_home"] == expected
    assert seen["status_home"] == expected


# ---------------------------------------------------------------------------
# Route-pinned CSRF/Host coverage: PUT /api/packs/{name} is the one endpoint
# that triggers a subprocess + remote fetch, so its middleware protection is
# asserted BY NAME here. A future middleware path-exclusion or router-order
# change must fail this test, not silently drop the guard.
# ---------------------------------------------------------------------------

def _make_guarded_app():
    from jacked.api.security import HostValidationMiddleware, build_allowed_origins

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.add_middleware(HostValidationMiddleware)
    app.state.allowed_origins = build_allowed_origins("127.0.0.1", 8321)
    return app


def test_put_packs_foreign_origin_is_403(monkeypatch):
    monkeypatch.setattr(packs, "load_registry", lambda data_root: _sample_registry())
    client = TestClient(_make_guarded_app())
    resp = client.put(
        "/api/packs/marketing",
        json={"enabled": True},
        headers={"origin": "http://evil.example.com"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "CSRF_ORIGIN"


def test_put_packs_untrusted_host_is_421(monkeypatch):
    monkeypatch.setattr(packs, "load_registry", lambda data_root: _sample_registry())
    client = TestClient(_make_guarded_app())
    resp = client.put(
        "/api/packs/marketing",
        json={"enabled": True},
        headers={"host": "evil.example.com"},
    )
    assert resp.status_code == 421
    assert resp.json()["error"]["code"] == "UNTRUSTED_HOST"
