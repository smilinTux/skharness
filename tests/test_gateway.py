from fastapi.testclient import TestClient

from skharness.gateway import build_app
from skharness.manager import SessionManager
from skharness.registry import SessionRegistry
from skharness.spawner import FakeSpawner


def _client(tmp_path, *, verifier=lambda token: token == "good"):
    mgr = SessionManager(registry=SessionRegistry(path=tmp_path / "s.json"),
                         spawner=FakeSpawner())
    return TestClient(build_app(manager=mgr, verify_caller=verifier))


def test_spawn_list_attach_kill_flow(tmp_path):
    c = _client(tmp_path)
    h = {"authorization": "Bearer good"}
    r = c.post("/sessions", json={"agent": "lumina", "prompt": "do x", "repo": "/r"},
               headers=h)
    assert r.status_code == 200
    sid = r.json()["id"]
    assert r.json()["status"] == "running"

    assert any(s["id"] == sid for s in c.get("/sessions", headers=h).json()["sessions"])
    att = c.get(f"/sessions/{sid}/attach", headers=h)
    assert att.status_code == 200 and att.json()["web_url"].startswith("http")
    assert c.delete(f"/sessions/{sid}", headers=h).status_code == 200
    assert c.get("/sessions", headers=h).json()["sessions"] == []


def test_unauthenticated_rejected(tmp_path):
    c = _client(tmp_path)
    assert c.get("/sessions").status_code == 401                       # no token
    assert c.get("/sessions", headers={"authorization": "Bearer bad"}).status_code == 403
