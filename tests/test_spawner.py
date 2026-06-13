import pytest

from skharness.session import Session
from skharness.spawner import FakeSpawner


@pytest.mark.asyncio
async def test_fake_spawn_returns_worktree_tmux_weburl():
    sp = FakeSpawner()
    s = Session(id="s1", agent="lumina", repo="/r")
    out = await sp.spawn(s, prompt="do x")
    assert out.worktree.endswith("s1")
    assert out.tmux == "sk-s1"
    assert out.web_url.startswith("http")
    assert sp.spawned == ["s1"]


@pytest.mark.asyncio
async def test_fake_kill_records():
    sp = FakeSpawner()
    await sp.kill("s1")
    assert sp.killed == ["s1"]
