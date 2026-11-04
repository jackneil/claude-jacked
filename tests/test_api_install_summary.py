import json
from fastapi.testclient import TestClient
from jacked.api.main import app


def test_install_summary_absent(tmp_path, monkeypatch):
    # An empty jacked home holds no last-install record.
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    client = TestClient(app)
    r = client.get("/api/install/summary")
    assert r.status_code == 200
    assert r.json()["summary"] is None


def test_install_summary_present(tmp_path, monkeypatch):
    """The route reads where `jacked install` writes the record: .claude under
    the $JACKED_HOME-aware jacked home, resolved on each request."""
    p = tmp_path / ".claude" / "jacked-last-install.json"
    p.parent.mkdir()
    p.write_text(json.dumps({"to_version": "0.51.0", "from_version": "0.50.0",
                             "changes": {}, "unchanged_count": 3, "at": "x"}), encoding="utf-8")
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    client = TestClient(app)
    r = client.get("/api/install/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["summary"]["to_version"] == "0.51.0"
    assert body["mtime_iso"]
