from __future__ import annotations

import threading
from datetime import datetime, timezone

from skharness.arena.models import canonical_digest
from skharness.arena.swarm import (
    BudgetUsage,
    ExecutionBudget,
    ScoutAssessment,
    ScoutFinding,
    SubagentContract,
    SubagentDisposition,
    SubagentResult,
    SwarmIdentity,
    SwarmPhaseSpec,
    SwarmPlan,
    SwarmRole,
    TeamBudget,
)
from skharness.arena.swarm_control import SwarmScheduler
from skharness.arena.swarm_orchestrator import (
    A2AJournal,
    SwarmTopologyPolicy,
    TrustedSwarmOrchestrator,
    WorkerExecution,
)
from skharness.arena.swarm_verifier import (
    CheckProvenance,
    CriterionEvidence,
    EvidenceSource,
    SwarmCompletionGate,
    VerifierAttestation,
    VerifierVerdict,
    phase_lineage_digest,
)
from skharness.arena.trajectory import CardSize

NOW = datetime(2026, 8, 21, tzinfo=timezone.utc)
DIGEST = "sha256:" + "1" * 64
EVIDENCE = "sha256:" + "2" * 64
COMMIT = "a" * 40
RESULT_COMMIT = "b" * 40


def _identity() -> SwarmIdentity:
    return SwarmIdentity(
        card_id="card-1",
        card_hash=DIGEST,
        base_commit=COMMIT,
        evidence_id=DIGEST,
        trajectory_id="traj-1",
    )


def _plan(*roles: SwarmRole, counts: dict[SwarmRole, int] | None = None) -> SwarmPlan:
    counts = counts or {}
    phases = []
    prior_phase = None
    for role in roles:
        phase_id = f"phase-{role.value}"
        predecessors = (prior_phase,) if prior_phase is not None else ()
        phases.append(
            SwarmPhaseSpec(
                phase_id=phase_id,
                role=role,
                contract_ids=tuple(
                    f"{role.value}-{number}"
                    for number in range(1, counts.get(role, 1) + 1)
                ),
                predecessor_phase_ids=predecessors,
            )
        )
        prior_phase = phase_id
    return SwarmPlan(plan_id="plan-1", identity=_identity(), phases=tuple(phases), created_at=NOW)


def _contract(role: SwarmRole, plan: SwarmPlan, number: int = 1) -> SubagentContract:
    return SubagentContract(
        contract_id=f"{role.value}-{number}",
        team_id="team-1",
        identity=_identity(),
        plan_hash=plan.content_hash,
        phase_id=f"phase-{role.value}",
        parent_agent_id="orchestrator",
        child_agent_id=f"{role.value}-agent-{number}",
        role=role,
        task=f"Perform bounded {role.value} work.",
        readable_paths=("src", "tests"),
        writable_paths=("src",) if role is SwarmRole.BUILDER else (),
        protected_paths=("tests/hidden",),
        tool_allowlist=("rg", "pytest"),
        budget=ExecutionBudget(
            wall_seconds=60, token_limit=1000, tool_call_limit=20, cost_limit=1
        ),
        lease_id=f"lease-{role.value}-{number}",
        worktree_id=f"worktree-{number}" if role is SwarmRole.BUILDER else "worktree-main",
        issued_at=NOW,
    )


def _result(contract: SubagentContract, disposition=SubagentDisposition.COMPLETED):
    finding = ScoutFinding.create(
        path="src/skharness/arena/swarm.py",
        line=129,
        detail="The downstream contract needs immutable scout phase evidence.",
    )
    fields = dict(
        disposition=disposition,
        summary=f"{contract.role.value} terminal result",
        started_at=NOW,
        finished_at=NOW,
    )
    if disposition is SubagentDisposition.COMPLETED:
        fields["evidence_refs"] = (
            (EVIDENCE, finding.digest)
            if contract.role is SwarmRole.SCOUT
            else (EVIDENCE,)
        )
        if contract.role is SwarmRole.BUILDER:
            fields["observed_commit"] = RESULT_COMMIT
    else:
        fields["reason_codes"] = ("blocked_prerequisite",)
    if contract.role is SwarmRole.SCOUT:
        fields["scout_assessment"] = (
            ScoutAssessment.ACTIONABLE
            if disposition is SubagentDisposition.COMPLETED
            else ScoutAssessment.BLOCKED
        )
        fields["scout_findings"] = (
            (finding,) if disposition is SubagentDisposition.COMPLETED else ()
        )
    return SubagentResult.from_contract(contract, **fields)


def _scheduler(tmp_path, *, clock=None):
    return SwarmScheduler(
        TeamBudget(
            team_id="team-1",
            wall_seconds=300,
            token_limit=5000,
            tool_call_limit=100,
            cost_limit=5,
            max_concurrency=3,
        ),
        identity=_identity(),
        orchestrator_id="orchestrator",
        state_path=tmp_path / "state.json",
        **({"clock": clock} if clock is not None else {}),
    )


def _gate(plan):
    return SwarmCompletionGate(
        plan=plan,
        required_criteria=("ac-1",),
        trusted_verifier_ids=("independent-verifier",),
        verify_signature=lambda item: item.signature == "valid",
    )


def _attestation(results, receipts):
    return VerifierAttestation(
        identity=_identity(),
        plan_hash=results[0].plan_hash,
        phase_lineage_digest=phase_lineage_digest(receipts),
        final_commit=receipts[-1].output_commit,
        verifier_agent_id="independent-verifier",
        verdict=VerifierVerdict.APPROVED,
        subject_result_hashes=tuple(canonical_digest(item) for item in results),
        criteria=(
            CriterionEvidence(
                criterion_id="ac-1",
                passed=True,
                artifact_digest=EVIDENCE,
                observed_by="independent-verifier",
                source=EvidenceSource.VERIFIER,
                test_provenance=CheckProvenance.PREEXISTING,
            ),
        ),
        created_at=NOW,
        signature="valid",
    )


def test_topology_keeps_small_single_and_scales_medium_large():
    assert not SwarmTopologyPolicy.select(CardSize.SMALL).uses_subagents
    medium = SwarmTopologyPolicy.select(CardSize.MEDIUM)
    assert (medium.scout_count, medium.builder_count, medium.tester_count) == (1, 1, 1)
    large = SwarmTopologyPolicy.select(
        CardSize.LARGE, independent_workstreams=3, cross_repository=True
    )
    assert (large.scout_count, large.builder_count, large.tester_count) == (2, 1, 1)
    assert large.reason == "large_parallel_scout_single_builder_until_trusted_integration"


def test_orchestrator_orders_roles_records_a2a_and_requires_verifier(tmp_path):
    plan = _plan(SwarmRole.SCOUT, SwarmRole.BUILDER, SwarmRole.TESTER)
    contracts = tuple(
        _contract(role, plan)
        for role in (SwarmRole.SCOUT, SwarmRole.BUILDER, SwarmRole.TESTER)
    )
    observed = []

    def execute(contract):
        observed.append(contract.role)
        return WorkerExecution(_result(contract), BudgetUsage(wall_seconds=1, tool_calls=1))

    report = TrustedSwarmOrchestrator(
        _scheduler(tmp_path),
        _gate(plan),
        A2AJournal(tmp_path / "a2a.jsonl"),
        plan,
    ).run(contracts, execute=execute, stop=lambda lease_id: None, attest=_attestation)

    assert observed == [SwarmRole.SCOUT, SwarmRole.BUILDER, SwarmRole.TESTER]
    assert report.completion.authorized
    assert len(report.a2a_event_digests) == 6
    assert len((tmp_path / "a2a.jsonl").read_text().splitlines()) == 6


def test_noncompleted_scout_cancels_downstream_and_gate_denies(tmp_path):
    plan = _plan(SwarmRole.SCOUT, SwarmRole.BUILDER)
    scout, builder = _contract(SwarmRole.SCOUT, plan), _contract(SwarmRole.BUILDER, plan)

    def execute(contract):
        return WorkerExecution(
            _result(contract, SubagentDisposition.BLOCKED), BudgetUsage(wall_seconds=1)
        )

    report = TrustedSwarmOrchestrator(
        _scheduler(tmp_path),
        _gate(plan),
        A2AJournal(tmp_path / "a2a.jsonl"),
        plan,
    ).run(
        (scout, builder),
        execute=execute,
        stop=lambda lease_id: None,
        attest=lambda results, receipts: None,
    )

    assert [item.role for item in report.results] == [SwarmRole.SCOUT]
    assert not report.completion.authorized
    assert "phase_not_completed:phase-scout" in report.failure_reasons
    assert "planned_result_set_mismatch" in report.completion.reasons


def test_hard_deadline_stops_worker_and_gate_fails_closed(tmp_path):
    current = [0.0]
    stopped = threading.Event()
    plan = _plan(SwarmRole.BUILDER)
    builder = _contract(SwarmRole.BUILDER, plan)

    def execute(contract):
        current[0] = 61.0
        stopped.wait(2)
        return WorkerExecution(_result(contract), BudgetUsage(wall_seconds=60))

    def stop(lease_id):
        assert lease_id == builder.lease_id
        stopped.set()
        return True

    control = _scheduler(tmp_path, clock=lambda: current[0])
    report = TrustedSwarmOrchestrator(
        control,
        _gate(plan),
        A2AJournal(tmp_path / "a2a.jsonl"),
        plan,
    ).run((builder,), execute=execute, stop=stop, attest=lambda results, receipts: None)

    assert stopped.is_set()
    assert not report.completion.authorized
    assert report.results[0].disposition is SubagentDisposition.FAILED
    assert "hard_deadline_exceeded" in report.results[0].reason_codes


def test_nonquiescent_stop_is_not_acknowledged(tmp_path):
    current = [0.0]
    released = threading.Event()
    plan = _plan(SwarmRole.BUILDER)
    builder = _contract(SwarmRole.BUILDER, plan)

    def execute(contract):
        current[0] = 61.0
        released.wait(1)
        return WorkerExecution(_result(contract), BudgetUsage(wall_seconds=60))

    def stop(_lease_id):
        released.set()
        return False

    control = _scheduler(tmp_path, clock=lambda: current[0])
    report = TrustedSwarmOrchestrator(
        control,
        _gate(plan),
        A2AJournal(tmp_path / "a2a.jsonl"),
        plan,
    ).run((builder,), execute=execute, stop=stop, attest=lambda _results, _receipts: None)

    assert f"worker_stop_not_quiescent:{builder.contract_id}" in report.failure_reasons
    assert control.stop_requests() == (builder.lease_id,)


def test_missing_timed_out_scout_results_never_admit_builder(tmp_path):
    current = [0.0]
    released = threading.Event()
    stopped = []
    plan = _plan(
        SwarmRole.SCOUT,
        SwarmRole.BUILDER,
        counts={SwarmRole.SCOUT: 2},
    )
    contracts = (
        _contract(SwarmRole.SCOUT, plan, 1),
        _contract(SwarmRole.SCOUT, plan, 2),
        _contract(SwarmRole.BUILDER, plan),
    )
    executed = []

    def execute(contract):
        executed.append(contract.contract_id)
        current[0] = 61.0
        released.wait(2)
        return WorkerExecution(_result(contract), BudgetUsage(wall_seconds=60))

    def stop(lease_id):
        stopped.append(lease_id)
        if len(stopped) == 2:
            released.set()
        return True

    control = _scheduler(tmp_path, clock=lambda: current[0])
    report = TrustedSwarmOrchestrator(
        control,
        _gate(plan),
        A2AJournal(tmp_path / "a2a.jsonl"),
        plan,
    ).run(contracts, execute=execute, stop=stop, attest=lambda results, receipts: None)

    assert set(executed) == {"scout-1", "scout-2"}
    assert "builder-1" not in executed
    assert len(stopped) == 2
    assert all(
        control.lease(f"lease-scout-{number}").result is not None
        for number in (1, 2)
    )
    assert all(item.controller_terminal_reason == "hard_deadline_exceeded"
               for item in report.results)
    assert "phase_completed_cardinality_mismatch:phase-scout" in report.failure_reasons
    assert not report.completion.authorized


def test_worker_exception_returns_failed_report_and_cancels_downstream(tmp_path):
    plan = _plan(SwarmRole.SCOUT, SwarmRole.BUILDER)
    contracts = (
        _contract(SwarmRole.SCOUT, plan),
        _contract(SwarmRole.BUILDER, plan),
    )
    executed = []

    def execute(contract):
        executed.append(contract.contract_id)
        raise RuntimeError("worker crashed")

    report = TrustedSwarmOrchestrator(
        _scheduler(tmp_path),
        _gate(plan),
        A2AJournal(tmp_path / "a2a.jsonl"),
        plan,
    ).run(contracts, execute=execute, stop=lambda lease_id: None,
          attest=lambda results, receipts: None)

    assert executed == ["scout-1"]
    assert any(item.startswith("worker_exception:scout-1") for item in report.failure_reasons)
    assert not report.completion.authorized


def test_terminal_budget_overage_becomes_structured_failed_report(tmp_path):
    plan = _plan(SwarmRole.BUILDER)
    builder = _contract(SwarmRole.BUILDER, plan)

    def execute(contract):
        return WorkerExecution(
            _result(contract),
            BudgetUsage(wall_seconds=1, tool_calls=contract.budget.tool_call_limit + 1),
        )

    report = TrustedSwarmOrchestrator(
        _scheduler(tmp_path),
        _gate(plan),
        A2AJournal(tmp_path / "a2a.jsonl"),
        plan,
    ).run((builder,), execute=execute, stop=lambda lease_id: None,
          attest=lambda results, receipts: None)

    assert report.results[0].disposition is SubagentDisposition.FAILED
    assert "budget_exceeded" in report.results[0].reason_codes
    assert any(item.startswith("worker_over_budget:builder-1") for item in report.failure_reasons)
    assert not report.completion.authorized
