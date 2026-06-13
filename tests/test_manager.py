import pytest

from skharness.manager import SessionManager
from skharness.registry import SessionRegistry
from skharness.session import Session, SessionStatus
from skharness.spawner import FakeSpawner, Spawner


def _mgr(tmp_path):
    return SessionManager(registry=SessionRegistry(path=tmp_path / "s.json"),
                          spawner=FakeSpawner())


class _FailingSpawner(Spawner):
    async def spawn(self, session: Session, *, prompt: str) -> Session:
        raise RuntimeError("boom")

    async def kill(self, session_id: str) -> None: ...


@pytest.mark.asyncio
async def test_spawn_registers_running_session_with_attach_url(tmp_path):
    m = _mgr(tmp_path)
    s = await m.spawn(agent="lumina", prompt="do x", repo="/r")
    assert s.status == SessionStatus.RUNNING
    assert s.web_url.startswith("http")
    assert m.attach_url(s.id) == s.web_url
    assert any(x.id == s.id for x in m.list())


@pytest.mark.asyncio
async def test_kill_ends_session(tmp_path):
    m = _mgr(tmp_path)
    s = await m.spawn(agent="a", prompt="p", repo="/r")
    await m.kill(s.id)
    assert m.registry.get(s.id).status == SessionStatus.ENDED
    assert m.list() == []


@pytest.mark.asyncio
async def test_attach_unknown_returns_none(tmp_path):
    assert _mgr(tmp_path).attach_url("nope") is None


@pytest.mark.asyncio
async def test_spawn_failure_leaves_no_ghost_session(tmp_path):
    m = SessionManager(registry=SessionRegistry(path=tmp_path / "s.json"),
                       spawner=_FailingSpawner())
    with pytest.raises(RuntimeError):
        await m.spawn(agent="lumina", prompt="do x", repo="/r")
    # No SPAWNING ghost lingers in live() — it was marked ENDED.
    assert m.list() == []
