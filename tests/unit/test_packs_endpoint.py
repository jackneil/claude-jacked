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
    return {
        "marketing": packs.Pack(
            name="marketing",
            display_name="Marketing Skills",
            description="Marketing bundle",
            source="coreyhaines31/marketingskills",
            homepage="https://github.com/coreyhaines31/marketingskills",
            skills=("ads", "seo"),
        ),
        "design-extras": packs.Pack(
            name="design-extras",
            display_name="Design Extras",
            description="Design bundle",
            source="emilkowalski/skills",
            homepage="https://github.com/emilkowalski/skills",
            skills=("improve-animations",),
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
    """GET returns npx_available plus every registry pack (sorted by name)
    with an enabled flag reflecting enabled_pack_names."""
    monkeypatch.setattr(packs, "load_registry", lambda data_root: _sample_registry())
    monkeypatch.setattr(packs, "enabled_pack_names", lambda home: ["marketing"])
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
    assert by_name["marketing"]["enabled"] is True
    assert by_name["design-extras"]["enabled"] is False
    # pack_status fields are carried through.
    assert by_name["marketing"]["total"] == 2
    assert by_name["marketing"]["installed_count"] == 0


def test_get_packs_npx_unavailable(monkeypatch):
    """npx_available is False when find_npx returns None."""
    monkeypatch.setattr(packs, "load_registry", lambda data_root: _sample_registry())
    monkeypatch.setattr(packs, "enabled_pack_names", lambda home: [])
    monkeypatch.setattr(packs, "pack_status", _fake_pack_status)
    monkeypatch.setattr(packs, "find_npx", lambda: None)

    client = TestClient(_make_app())
    body = client.get("/api/packs").json()
    assert body["npx_available"] is False
    assert all(p["enabled"] is False for p in body["packs"])


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


def test_put_disable_removes_then_sets_disabled_even_when_remove_fails(monkeypatch):
    """Disable runs remove_pack THEN set_enabled(False), and records disabled
    intent even when the remove op reports failure."""
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

    # remove first, then disabled intent recorded — regardless of remove result.
    assert order == [("remove", "marketing"), ("set_enabled", "marketing", False)]


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
