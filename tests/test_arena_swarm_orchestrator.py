from __future__ import annotations

import threading
from datetime import datetime, timezone

from skharness.arena.models import canonical_digest
from skharness.arena.swarm import (
    BudgetUsage,
    ExecutionBudget,
    SubagentContract,
    SubagentDisposition,
    SubagentResult,
    SwarmIdentity,
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
)
from skharness.arena.trajectory import CardSize

NOW = datetime(2026, 8, 21, tzinfo=timezone.utc)
DIGEST = "sha256:" + "1" * 64
EVIDENCE = "sha256:" + "2" * 64
COMMIT = "a" * 40


def _identity() -> SwarmIdentity:
    return SwarmIdentity(
        card_id="card-1",
        card_hash=DIGEST,
        base_commit=COMMIT,
        evidence_id=DIGEST,
        trajectory_id="traj-1",
    )


def _contract(role: SwarmRole, number: int = 1) -> SubagentContract:
    return SubagentContract(
        contract_id=f"{role.value}-{number}",
        team_id="team-1",
        identity=_identity(),
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
    fields = dict(
        disposition=disposition,
        summary=f"{contract.role.value} terminal result",
        started_at=NOW,
        finished_at=NOW,
    )
    if disposition is SubagentDisposition.COMPLETED:
        fields["evidence_refs"] = (EVIDENCE,)
        if contract.role is SwarmRole.BUILDER:
            fields["observed_commit"] = COMMIT
    else:
        fields["reason_codes"] = ("blocked_prerequisite",)
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


def _gate(required_roles):
    return SwarmCompletionGate(
        identity=_identity(),
        required_roles=required_roles,
        required_criteria=("ac-1",),
        trusted_verifier_ids=("independent-verifier",),
        verify_signature=lambda item: item.signature == "valid",
    )


def _attestation(results):
    return VerifierAttestation(
        identity=_identity(),
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
    assert (large.scout_count, large.builder_count, large.tester_count) == (2, 3, 1)


def test_orchestrator_orders_roles_records_a2a_and_requires_verifier(tmp_path):
    contracts = tuple(_contract(role) for role in (SwarmRole.SCOUT, SwarmRole.BUILDER,
                                                    SwarmRole.TESTER))
    observed = []

    def execute(contract):
        observed.append(contract.role)
        return WorkerExecution(_result(contract), BudgetUsage(wall_seconds=1, tool_calls=1))

    report = TrustedSwarmOrchestrator(
        _scheduler(tmp_path),
        _gate({SwarmRole.SCOUT, SwarmRole.BUILDER, SwarmRole.TESTER}),
        A2AJournal(tmp_path / "a2a.jsonl"),
    ).run(contracts, execute=execute, stop=lambda lease_id: None, attest=_attestation)

    assert observed == [SwarmRole.SCOUT, SwarmRole.BUILDER, SwarmRole.TESTER]
    assert report.completion.authorized
    assert len(report.a2a_event_digests) == 6
    assert len((tmp_path / "a2a.jsonl").read_text().splitlines()) == 6


def test_noncompleted_scout_cancels_downstream_and_gate_denies(tmp_path):
    scout, builder = _contract(SwarmRole.SCOUT), _contract(SwarmRole.BUILDER)

    def execute(contract):
        return WorkerExecution(
            _result(contract, SubagentDisposition.BLOCKED), BudgetUsage(wall_seconds=1)
        )

    report = TrustedSwarmOrchestrator(
        _scheduler(tmp_path),
        _gate({SwarmRole.SCOUT, SwarmRole.BUILDER}),
        A2AJournal(tmp_path / "a2a.jsonl"),
    ).run(
        (scout, builder),
        execute=execute,
        stop=lambda lease_id: None,
        attest=lambda results: None,
    )

    assert [item.role for item in report.results] == [SwarmRole.SCOUT]
    assert not report.completion.authorized
    assert "worker_not_completed" in report.completion.reasons


def test_hard_deadline_stops_worker_and_gate_fails_closed(tmp_path):
    current = [0.0]
    stopped = threading.Event()
    builder = _contract(SwarmRole.BUILDER)

    def execute(contract):
        current[0] = 61.0
        stopped.wait(2)
        return WorkerExecution(_result(contract), BudgetUsage(wall_seconds=60))

    def stop(lease_id):
        assert lease_id == builder.lease_id
        stopped.set()

    report = TrustedSwarmOrchestrator(
        _scheduler(tmp_path, clock=lambda: current[0]),
        _gate({SwarmRole.BUILDER}),
        A2AJournal(tmp_path / "a2a.jsonl"),
    ).run((builder,), execute=execute, stop=stop, attest=lambda results: None)

    assert stopped.is_set()
    assert not report.completion.authorized
    assert report.results == ()
