"""PoolController: spawn / scale / drain / destroy over the REAL guarded spawn
path (ClaudeCodeHarness), with a fake tmux/git runner so no real tmux or git
process ever runs. This deliberately reuses the harness's own fake-runner
style (see test_claude_code_harness.py) rather than mocking PoolController's
collaborator away, so the allowlist-refusal test below actually exercises
ClaudeCodeHarness.spawn's own guard, not an assumption about it.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from skharness.activity import ActivityJournal, ActivityKind
from skharness.harness import SpawnRejected
from skharness.harnesses.claude_code import ClaudeCodeHarness
from skharness.pool import PoolController


class FakeTmux:
    """A stateful fake tmux runner: tracks which sids are "live" windows so a
    spawn -> archive lifecycle behaves like the real harness/tmux would,
    without ever touching a real tmux server."""

    def __init__(self) -> None:
        self.live: set[str] = set()
        self.calls: list[list[str]] = []
        self.transcripts: dict[str, str] = {}

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(list(argv))
        if "new-session" in argv:
            return ""
        if "new-window" in argv:
            sid = argv[argv.index("-n") + 1]
            self.live.add(sid)
            return ""
        if "set-window-option" in argv:
            return ""
        if "pipe-pane" in argv:
            return ""
        if "list-windows" in argv:
            lines = ["monitor\t1700000000"] + [
                f"{sid}\t1700000100" for sid in sorted(self.live)
            ]
            return "\n".join(lines) + "\n"
        if "capture-pane" in argv:
            target = argv[argv.index("-t") + 1]
            sid = target.rsplit(":", 1)[-1]
            return self.transcripts.get(sid, f"transcript for {sid}\n")
        if "kill-window" in argv:
            target = argv[argv.index("-t") + 1]
            sid = target.rsplit(":", 1)[-1]
            self.live.discard(sid)
            return ""
        raise AssertionError(f"unexpected tmux call: {argv}")


def _git_ok():
    calls: list[list[str]] = []

    def git(argv):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    return git, calls


def _harness(repo_root: Path, *, runner: FakeTmux, git_runner, sessions_root: Path):
    return ClaudeCodeHarness(
        host=".158",
        runner=runner,
        git_runner=git_runner,
        dispatch_repos=[str(repo_root)],
        worktree_root=repo_root.parent / "wt",
        sessions_root=sessions_root,
        claude_bin="claude",
        child_path="/usr/bin:/bin",
    )


def _controller(harness, tmp_path: Path) -> PoolController:
    activity = ActivityJournal(root=tmp_path / "activity")
    return PoolController(harness, activity=activity)


# --- the drill (acceptance criterion 1 + 2) ----------------------------------


@pytest.mark.asyncio
async def test_drill_spawn_two_drain_one_destroy_pool_transcripts_persist(tmp_path):
    """Spawns two panes against a fixture repo on the allowlist, drains one,
    destroys the pool, and asserts every transcript persisted."""
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    sessions_root = tmp_path / "agents"
    tmux = FakeTmux()
    git, gcalls = _git_ok()
    h = _harness(repo, runner=tmux, git_runner=git, sessions_root=sessions_root)
    pool = _controller(h, tmp_path)

    m1 = await pool.spawn(lane="fixture", repo=str(repo), branch="main", prompt="do task 1")
    m2 = await pool.spawn(lane="fixture", repo=str(repo), branch="main", prompt="do task 2")

    assert m1.sid != m2.sid
    assert {m1.sid, m2.sid} == tmux.live
    assert len(pool.members("fixture")) == 2

    # drain one
    drain_result = await pool.drain(m1.sid)
    assert drain_result["archived"] is True
    assert m1.sid not in tmux.live
    assert m2.sid in tmux.live
    transcript_path_1 = Path(drain_result["transcript_path"])
    assert transcript_path_1.exists()

    # destroy the pool (drains everything still active, i.e. m2)
    destroy_results = await pool.destroy()
    assert len(destroy_results) == 1
    assert destroy_results[0]["sid"] == m2.sid
    assert destroy_results[0]["archived"] is True
    assert m2.sid not in tmux.live

    # every transcript persisted: both members' drain_result carries a real path
    for member in pool.members("fixture"):
        assert member.drained is True
        path = Path(member.drain_result["transcript_path"])
        assert path.exists()
        record = json.loads(path.read_text())
        assert record["state"] == "archived"
        assert record["sid"] == member.sid

    # a second destroy is a safe no-op (already drained, skipped)
    assert await pool.destroy() == []


@pytest.mark.asyncio
async def test_spawn_against_repo_not_on_allowlist_refuses(tmp_path):
    """Exercises the REAL ClaudeCodeHarness.spawn allowlist guard (not an
    assumption): the controller does not track anything and never touches
    tmux/git when the harness itself refuses the repo."""
    allowed = tmp_path / "allowed-repo"
    allowed.mkdir()
    other = tmp_path / "not-allowed-repo"
    other.mkdir()
    tmux = FakeTmux()
    git, gcalls = _git_ok()
    h = _harness(allowed, runner=tmux, git_runner=git, sessions_root=tmp_path / "agents")
    pool = _controller(h, tmp_path)

    with pytest.raises(SpawnRejected, match="allowlist"):
        await pool.spawn(lane="fixture", repo=str(other), branch="main", prompt="p")

    assert pool.members() == []
    assert tmux.calls == []
    assert gcalls == []


@pytest.mark.asyncio
async def test_spawn_empty_allowlist_refuses_via_pool(tmp_path):
    """The allowlist's deny-by-default (empty allowlist) also propagates
    through the controller unchanged."""
    repo = tmp_path / "repo"
    repo.mkdir()
    tmux = FakeTmux()
    git, gcalls = _git_ok()
    h = ClaudeCodeHarness(
        host=".158", runner=tmux, git_runner=git,
        dispatch_repos=[], worktree_root=tmp_path / "wt",
        sessions_root=tmp_path / "agents",
    )
    pool = _controller(h, tmp_path)

    with pytest.raises(SpawnRejected, match="deny all"):
        await pool.spawn(lane="x", repo=str(repo), branch="main", prompt="p")
    assert pool.members() == []


# --- scale --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scale_up_spawns_missing_panes(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    tmux = FakeTmux()
    git, _ = _git_ok()
    h = _harness(repo, runner=tmux, git_runner=git, sessions_root=tmp_path / "agents")
    pool = _controller(h, tmp_path)

    members = await pool.scale(lane="a", repo=str(repo), branch="main", prompt="p", target=3)
    assert len(members) == 3
    assert len(tmux.live) == 3


@pytest.mark.asyncio
async def test_scale_down_drains_oldest_first(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    tmux = FakeTmux()
    git, _ = _git_ok()
    h = _harness(repo, runner=tmux, git_runner=git, sessions_root=tmp_path / "agents")
    pool = _controller(h, tmp_path)

    m1 = await pool.spawn(lane="a", repo=str(repo), branch="main", prompt="p")
    m1.spawned_at = 100.0
    m2 = await pool.spawn(lane="a", repo=str(repo), branch="main", prompt="p")
    m2.spawned_at = 200.0
    m3 = await pool.spawn(lane="a", repo=str(repo), branch="main", prompt="p")
    m3.spawned_at = 300.0

    members = await pool.scale(lane="a", repo=str(repo), branch="main", prompt="p", target=1)
    assert len(members) == 1
    assert members[0].sid == m3.sid
    assert m1.drained is True
    assert m2.drained is True
    assert m3.drained is False


@pytest.mark.asyncio
async def test_scale_never_touches_another_lane(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    tmux = FakeTmux()
    git, _ = _git_ok()
    h = _harness(repo, runner=tmux, git_runner=git, sessions_root=tmp_path / "agents")
    pool = _controller(h, tmp_path)

    other = await pool.spawn(lane="other", repo=str(repo), branch="main", prompt="p")
    await pool.scale(lane="mine", repo=str(repo), branch="main", prompt="p", target=2)

    assert other.drained is False
    assert other.sid in tmux.live
    assert len(pool.members("mine")) == 2


@pytest.mark.asyncio
async def test_scale_negative_target_rejected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    tmux = FakeTmux()
    git, _ = _git_ok()
    h = _harness(repo, runner=tmux, git_runner=git, sessions_root=tmp_path / "agents")
    pool = _controller(h, tmp_path)
    with pytest.raises(ValueError):
        await pool.scale(lane="a", repo=str(repo), branch="main", prompt="p", target=-1)


# --- drain scoping --------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_refuses_a_pane_this_controller_did_not_spawn(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    tmux = FakeTmux()
    git, _ = _git_ok()
    h = _harness(repo, runner=tmux, git_runner=git, sessions_root=tmp_path / "agents")
    pool = _controller(h, tmp_path)

    result = await pool.drain("someone-elses-session")
    assert result["archived"] is False
    assert "not a pool-tracked session" in result["reason"]
    # never touched tmux at all
    assert tmux.calls == []


# --- activity: authority is always "observation" -----------------------------


@pytest.mark.asyncio
async def test_spawn_and_drain_emit_observation_only_activity(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    tmux = FakeTmux()
    git, _ = _git_ok()
    activity_root = tmp_path / "activity"
    h = _harness(repo, runner=tmux, git_runner=git, sessions_root=tmp_path / "agents")
    journal = ActivityJournal(root=activity_root)
    pool = PoolController(h, activity=journal)

    member = await pool.spawn(lane="a", repo=str(repo), branch="main", prompt="p")
    await pool.drain(member.sid)

    events = journal.read_after(0)
    assert len(events) == 2
    kinds = {e.kind for e in events}
    assert ActivityKind.STATUS in kinds
    for event in events:
        assert event.authority == "observation"
        assert event.session_id == member.sid
        assert event.source == "lane1-pool"


@pytest.mark.asyncio
async def test_refused_spawn_emits_no_activity(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    tmux = FakeTmux()
    git, _ = _git_ok()
    h = _harness(allowed, runner=tmux, git_runner=git, sessions_root=tmp_path / "agents")
    journal = ActivityJournal(root=tmp_path / "activity")
    pool = PoolController(h, activity=journal)

    with pytest.raises(SpawnRejected):
        await pool.spawn(lane="a", repo=str(other), branch="main", prompt="p")

    assert journal.read_after(0) == []


# --- poll_board (read-only) ---------------------------------------------------


def test_poll_board_no_runner_returns_empty(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    tmux = FakeTmux()
    git, _ = _git_ok()
    h = _harness(repo, runner=tmux, git_runner=git, sessions_root=tmp_path / "agents")
    pool = PoolController(h)
    assert pool.poll_board() == []


def test_poll_board_filters_status_and_labels(tmp_path):
    board = {
        "lane1": {
            "doing": [
                {"id": "a1", "status": "doing", "labels": ["sklegal"]},
                {"id": "a2", "status": "doing", "labels": ["unrelated"]},
            ],
            "done": [{"id": "a3", "status": "done", "labels": ["sklegal"]}],
        }
    }

    def runner(argv):
        assert argv == ["skcapstone", "coord", "kanban", "--json"]
        return json.dumps(board)

    repo = tmp_path / "repo"
    repo.mkdir()
    tmux = FakeTmux()
    git, _ = _git_ok()
    h = _harness(repo, runner=tmux, git_runner=git, sessions_root=tmp_path / "agents")
    pool = PoolController(h, board_runner=runner)

    got = pool.poll_board(statuses=("doing",), labels=frozenset({"sklegal"}))
    assert [c["id"] for c in got] == ["a1"]


def test_poll_board_fails_soft_on_garbage_output(tmp_path):
    def runner(argv):
        return "not json at all {{{"

    repo = tmp_path / "repo"
    repo.mkdir()
    tmux = FakeTmux()
    git, _ = _git_ok()
    h = _harness(repo, runner=tmux, git_runner=git, sessions_root=tmp_path / "agents")
    pool = PoolController(h, board_runner=runner)
    assert pool.poll_board() == []


def test_poll_board_fails_soft_on_runner_raising(tmp_path):
    def runner(argv):
        raise OSError("coord CLI not found")

    repo = tmp_path / "repo"
    repo.mkdir()
    tmux = FakeTmux()
    git, _ = _git_ok()
    h = _harness(repo, runner=tmux, git_runner=git, sessions_root=tmp_path / "agents")
    pool = PoolController(h, board_runner=runner)
    assert pool.poll_board() == []
