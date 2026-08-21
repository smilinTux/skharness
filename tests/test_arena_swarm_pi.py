from __future__ import annotations

import json
from datetime import datetime, timezone

from skharness.arena.runner import PiExperimentRunner, RunOutcome
from skharness.arena.scheduler import AttemptRequest
from skharness.arena.swarm import (
    ExecutionBudget,
    PhaseInput,
    ScoutFinding,
    SubagentContract,
    SubagentDisposition,
    SwarmIdentity,
    SwarmRole,
)
from skharness.arena.swarm_pi import PiSwarmLaunch, PiSwarmWorkerRuntime
from skharness.arena.trajectory import CardSize
from skharness.autocode.sandbox import InspectionScope, LaunchSpec

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
        plan_hash=DIGEST,
        phase_id=f"phase-{role.value}",
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
        self.spec = None

    def execute(self, request, spec, **kwargs):
        self.kwargs = kwargs
        self.spec = spec
        return self.outcome


def _launch(contract):
    return PiSwarmLaunch(
        request=AttemptRequest("challenge", "experiment", "1", contract.contract_id),
        spec=LaunchSpec(
            "pi",
            ["pi", "-p", "bounded task"],
            "image",
            "/work",
            inspection_scope=InspectionScope(max_calls=48),
        ),
        card_size=CardSize.MEDIUM,
        requested_model="ornith",
        timeout_s=600,
    )


def test_pi_runtime_maps_success_to_evidence_not_completion_authority():
    runner = Runner(
        RunOutcome(True, "exit", 0, DIGEST, "sha256:" + "2" * 64,
                   metrics={"duration_s": 1.2, "tokens": 80, "tool_calls": 4, "cost": 0.5})
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
    assert execution.usage.tokens == 80
    assert execution.usage.tool_calls == 4
    assert execution.usage.cost == 0.5
    assert runner.kwargs["timeout_s"] == 60
    assert runner.spec.scoped_readable_paths == ["src"]
    assert runner.spec.scoped_writable_paths == ["src"]
    assert runner.spec.inspection_scope.max_calls == 10


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
    assert execution.result.scout_assessment.value == "blocked"
    assert "SCOUT_ASSESSMENT:" in runner.spec.argv[2]
    runtime._active["lease-1"] = runner
    runtime.stop("lease-1")
    assert runner.supervisor.cancelled


def test_pi_runtime_never_infers_actionable_scout_from_zero_exit():
    runner = Runner(
        RunOutcome(True, "exit", 0, DIGEST, "sha256:" + "2" * 64)
    )
    runtime = PiSwarmWorkerRuntime(
        runner_factory=lambda contract: runner,
        launch_factory=_launch,
        observe_commit=lambda contract: COMMIT,
    )
    execution = runtime.execute(_contract(SwarmRole.SCOUT))

    assert execution.result.disposition is SubagentDisposition.BLOCKED
    assert execution.result.scout_assessment.value == "blocked"
    assert "scout_assessment_missing_or_invalid" in execution.result.reason_codes


def test_pi_runtime_accepts_only_parsed_path_scoped_scout_evidence():
    finding = ScoutFinding.create(
        path="src/skharness/arena/swarm.py",
        line=129,
        detail="The phase lineage contract requires immutable inputs.",
    )
    runner = Runner(
        RunOutcome(
            True,
            "exit",
            0,
            DIGEST,
            "sha256:" + "2" * 64,
            scout_assessment="actionable",
            scout_findings=(finding.model_dump(mode="json"),),
        )
    )
    runtime = PiSwarmWorkerRuntime(
        runner_factory=lambda contract: runner,
        launch_factory=_launch,
        observe_commit=lambda contract: COMMIT,
    )
    execution = runtime.execute(_contract(SwarmRole.SCOUT))

    assert execution.result.disposition is SubagentDisposition.COMPLETED
    assert execution.result.scout_assessment.value == "actionable"
    assert finding.digest in execution.result.evidence_refs
    assert execution.result.scout_findings == (finding,)


def test_runner_scout_parser_requires_exact_heading_and_concrete_path(tmp_path):
    path = tmp_path / "stdout.log"

    def write_final(text):
        path.write_text(
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": text}],
                    },
                }
            )
            + "\n"
        )

    write_final(
        "SCOUT_ASSESSMENT: ACTIONABLE\n"
        "SCOUT_FINDING: src/skharness/arena/swarm.py:129 - contract lineage is missing"
    )
    assessment, findings = PiExperimentRunner._pi_scout_terminal(path)
    assert assessment == "actionable"
    assert len(findings) == 1 and str(findings[0]["digest"]).startswith("sha256:")

    write_final("SCOUT_ASSESSMENT: ACTIONABLE\nSCOUT_FINDING: none")
    assert PiExperimentRunner._pi_scout_terminal(path) == (None, ())


def test_builder_prompt_receives_only_typed_predecessor_scout_findings():
    finding = ScoutFinding.create(
        path="src/skharness/arena/swarm.py",
        line=129,
        detail="The builder must consume the exact scout lineage contract.",
    )
    builder = _contract(SwarmRole.BUILDER)
    phase_input = PhaseInput(
        source_phase_id="phase-scout",
        source_contract_id="scout-1",
        source_contract_hash=DIGEST,
        source_role=SwarmRole.SCOUT,
        source_receipt_hash="sha256:" + "4" * 64,
        source_result_hash="sha256:" + "5" * 64,
        identity_hash=builder.identity.content_hash,
        evidence_refs=(finding.digest,),
        scout_findings=(finding,),
        output_commit=COMMIT,
    )
    bound = SubagentContract.model_validate(
        builder.model_dump()
        | {"phase_inputs": (phase_input,), "inputs_bound_at": builder.issued_at}
    )

    argv = PiSwarmWorkerRuntime._bounded_argv(bound, _launch(bound).spec)
    prompt = argv[2]
    assert finding.path in prompt
    assert finding.detail in prompt
    assert finding.digest in prompt
