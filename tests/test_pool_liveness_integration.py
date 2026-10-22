"""Card 7c146c9c: exact-executable pool liveness integration qualification."""

from __future__ import annotations

import pytest

from skharness.activity import ActivityJournal, ActivityKind
from skharness.harness import HarnessSession
from skharness.harnesses.claude_code import ClaudeCodeHarness, CommandResult
from skharness.pool import PoolController


class ExactPaneRunner:
    def __init__(self) -> None:
        self.panes: dict[str, tuple[str, str, str]] = {}
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> CommandResult:
        self.calls.append(list(argv))
        if argv[:2] != ["tmux", "list-panes"]:
            raise AssertionError(f"unexpected command: {argv}")
        resource_id = argv[argv.index("-t") + 1]
        pane_pid, executable, pane_dead = self.panes[resource_id]
        stdout = f"{resource_id}\\t{pane_pid}\\t{executable}\\t{pane_dead}"
        return CommandResult(tuple(argv), 0, stdout, "")


class IntegratedHarness(ClaudeCodeHarness):
    """No-process collaborator retaining the production exact probe unchanged."""

    def __init__(self, runner: ExactPaneRunner) -> None:
        super().__init__(runner=runner)
        self.panes = runner.panes
        self.spawn_count = 0
        self.archived: list[str] = []

    async def spawn_reserved(self, desc, *, prompt, excluded_sids):
        del prompt
        self.spawn_count += 1
        sid = f"agent-{self.spawn_count:032x}"
        assert sid not in excluded_sids
        resource_id = f"@{self.spawn_count}"
        self._spawned_sids.add(sid)
        self._resource_ids[sid] = resource_id
        self._spawned_executables[sid] = "pi"
        # A healthy idle pane: exact pane executable and no child-process fact.
        self.panes[resource_id] = (str(1000 + self.spawn_count), "pi", "0")
        return HarnessSession(
            sid=sid,
            descriptor=desc,
            status="running",
            resource_id=resource_id,
        )

    async def archive(self, sid: str) -> dict:
        self.archived.append(sid)
        return {
            "sid": sid,
            "archived": True,
            "transcript_path": f"/typed/fake/{sid}.json",
        }


@pytest.mark.asyncio
async def test_fifty_idle_exact_panes_then_one_bare_shell_reap_and_refill(tmp_path):
    runner = ExactPaneRunner()
    harness = IntegratedHarness(runner)
    journal = ActivityJournal(root=tmp_path / "activity")
    pool = PoolController(harness, activity=journal, clock=lambda: 1787774400.0)

    active = await pool.scale(
        lane="review",
        repo="repo",
        branch="branch",
        prompt="idle",
        target=50,
    )
    assert len(active) == 50

    # Fifty healthy idle exact-executable panes remain active and reap zero.
    retained = await pool.active_members("review")
    assert [member.sid for member in retained] == [member.sid for member in active]
    assert harness.archived == []
    assert [
        event for event in journal.read_after(0) if event.kind == ActivityKind.DISPOSITION
    ] == []

    victim = active[23]
    victim_resource = victim.session.resource_id
    runner.panes[victim_resource] = ("1024", "bash", "0")

    # Only the controller-owned exact-executable mismatch is excluded/reaped.
    after_mismatch = await pool.active_members("review")
    assert len(after_mismatch) == 49
    assert victim.sid not in {member.sid for member in after_mismatch}
    assert harness.archived == [victim.sid]

    refilled = await pool.scale(
        lane="review",
        repo="repo",
        branch="branch",
        prompt="replacement",
        target=50,
    )
    assert len(refilled) == 50
    assert victim.sid not in {member.sid for member in refilled}
    assert harness.spawn_count == 51
    assert harness.archived == [victim.sid]

    reap_events = [
        event for event in journal.read_after(0) if event.kind == ActivityKind.DISPOSITION
    ]
    assert len(reap_events) == 1
    assert reap_events[0].session_id == victim.sid
    assert reap_events[0].data["sid"] == victim.sid
    assert reap_events[0].data["reason"] == "expected executable mismatch"

    # No command in the integrated path can inspect process trees or children.
    assert len(runner.calls) == 149  # 50 + 50 + 49 probes; corpse is never reprobed.
    assert {tuple(call[:2]) for call in runner.calls} == {("tmux", "list-panes")}
    assert all("pane_current_command" in call[-1] for call in runner.calls)
    assert all("child" not in " ".join(call).lower() for call in runner.calls)


@pytest.mark.asyncio
async def test_archive_failure_does_not_reap_drain_or_refill(tmp_path):
    class ArchiveFailureHarness(IntegratedHarness):
        async def archive(self, sid: str) -> dict:
            self.archived.append(sid)
            return {
                "sid": sid,
                "archived": False,
                "reason": "forced transcript archive failure",
            }

    runner = ExactPaneRunner()
    harness = ArchiveFailureHarness(runner)
    journal = ActivityJournal(root=tmp_path / "activity")
    pool = PoolController(harness, activity=journal, clock=lambda: 1787774400.0)

    member = await pool.spawn(
        lane="review", repo="repo", branch="branch", prompt="idle"
    )
    runner.panes[member.session.resource_id] = ("1001", "bash", "0")

    active = await pool.scale(
        lane="review",
        repo="repo",
        branch="branch",
        prompt="replacement",
        target=1,
    )

    assert active == [member]
    assert harness.spawn_count == 1
    assert harness.archived == [member.sid]
    assert member.reaped is False
    assert member.drained is False
    assert member.reap_result == {}
    assert member.drain_result == {}

    events = [
        event for event in journal.read_after(0) if event.kind == ActivityKind.DISPOSITION
    ]
    assert len(events) == 1
    assert events[0].session_id == member.sid
    assert events[0].summary == (
        f"lane1 pool failed to reap dead member {member.sid!r}: archive unsuccessful"
    )
    assert events[0].data == {
        "sid": member.sid,
        "reaped": False,
        "reason": "expected executable mismatch",
        "archive": {
            "sid": member.sid,
            "archived": False,
            "reason": "forced transcript archive failure",
        },
    }
    assert all("pane_current_command" in call[-1] for call in runner.calls)
    assert all("child" not in " ".join(call).lower() for call in runner.calls)
