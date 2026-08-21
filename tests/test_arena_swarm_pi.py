from __future__ import annotations

from datetime import datetime, timezone

from skharness.arena.runner import RunOutcome
from skharness.arena.scheduler import AttemptRequest
from skharness.arena.swarm import (
    ExecutionBudget,
    SubagentContract,
    SubagentDisposition,
    SwarmIdentity,
    SwarmRole,
)
from skharness.arena.swarm_pi import PiSwarmLaunch, PiSwarmWorkerRuntime
from skharness.arena.trajectory import CardSize
from skharness.autocode.sandbox import LaunchSpec

DIGEST = "sha256:" + "1" * 64
COMMIT = "a" * 40


def _contract(role=SwarmRole.BUILDER):
    return SubagentContract(
        contract_id="worker-1",
        team_id="team-1",
        identity=SwarmIdentity(
            card_id="card-1",
            card_hash=DIGEST,
            base_commit=COMMIT,
            evidence_id=DIGEST,
            trajectory_id="trajectory-1",
        ),
        parent_agent_id="orchestrator",
        child_agent_id="worker",
        role=role,
        task="bounded work",
        readable_paths=("src",),
        writable_paths=("src",) if role is SwarmRole.BUILDER else (),
        tool_allowlist=("rg",),
        budget=ExecutionBudget(
            wall_seconds=60, token_limit=100, tool_call_limit=10, cost_limit=1
        ),
        lease_id="lease-1",
        worktree_id="worktree-1",
        issued_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )


class Supervisor:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class Runner:
    def __init__(self, outcome):
        self.outcome = outcome
        self.supervisor = Supervisor()
        self.kwargs = None

    def execute(self, request, spec, **kwargs):
        self.kwargs = kwargs
        return self.outcome


def _launch(contract):
    return PiSwarmLaunch(
        request=AttemptRequest("challenge", "experiment", "1", contract.contract_id),
        spec=LaunchSpec("pi", ["pi"], "image", "/work"),
        card_size=CardSize.MEDIUM,
        requested_model="ornith",
        timeout_s=600,
    )


def test_pi_runtime_maps_success_to_evidence_not_completion_authority():
    runner = Runner(
        RunOutcome(True, "exit", 0, DIGEST, "sha256:" + "2" * 64,
                   metrics={"duration_s": 1.2})
    )
    runtime = PiSwarmWorkerRuntime(
        runner_factory=lambda contract: runner,
        launch_factory=_launch,
        observe_commit=lambda contract: COMMIT,
    )
    execution = runtime.execute(_contract())

    assert execution.result.disposition is SubagentDisposition.COMPLETED
    assert execution.result.evidence_refs == (DIGEST, "sha256:" + "2" * 64)
    assert execution.result.observed_commit == COMMIT
    assert execution.usage.wall_seconds == 2
    assert runner.kwargs["timeout_s"] == 60


def test_pi_runtime_preserves_blocked_disposition_and_can_cancel_active_runner():
    runner = Runner(
        RunOutcome(False, "blocked", 0, DIGEST, "sha256:" + "2" * 64,
                   partial=True, disposition="blocked")
    )
    runtime = PiSwarmWorkerRuntime(
        runner_factory=lambda contract: runner,
        launch_factory=_launch,
        observe_commit=lambda contract: COMMIT,
    )
    execution = runtime.execute(_contract(SwarmRole.SCOUT))
    assert execution.result.disposition is SubagentDisposition.BLOCKED
    assert execution.result.reason_codes == ("blocked",)

    runtime._active["lease-1"] = runner
    runtime.stop("lease-1")
    assert runner.supervisor.cancelled
