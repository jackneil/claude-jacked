import json
from fastapi.testclient import TestClient
from jacked.api.main import app


def test_install_summary_absent(tmp_path, monkeypatch):
    monkeypatch.setattr("jacked.install_summary.DEFAULT_LAST_INSTALL_PATH", tmp_path / "missing.json")
    # Re-point the route's module-level reference if it imported the constant directly:
    import jacked.api.routes.system as sysroutes
    monkeypatch.setattr(sysroutes, "_LAST_INSTALL_PATH", tmp_path / "missing.json", raising=False)
    client = TestClient(app)
    r = client.get("/api/install/summary")
    assert r.status_code == 200
    assert r.json()["summary"] is None


def test_install_summary_present(tmp_path, monkeypatch):
    p = tmp_path / "last.json"
    p.write_text(json.dumps({"to_version": "0.51.0", "from_version": "0.50.0",
                             "changes": {}, "unchanged_count": 3, "at": "x"}), encoding="utf-8")
    import jacked.api.routes.system as sysroutes
    monkeypatch.setattr(sysroutes, "_LAST_INSTALL_PATH", p, raising=False)
    client = TestClient(app)
    r = client.get("/api/install/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["to_version"] == "0.51.0"
    assert body["mtime_iso"]
