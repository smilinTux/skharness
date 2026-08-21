"""Fail-closed completion over exact controller-owned swarm phase lineage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from skharness.arena.swarm import (
    ExecutionBudget,
    PhaseReceipt,
    ScoutAssessment,
    ScoutFinding,
    SubagentContract,
    SubagentDisposition,
    SubagentResult,
    SwarmIdentity,
    SwarmPhaseSpec,
    SwarmPlan,
    SwarmRole,
    bind_phase_inputs,
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

NOW = datetime(2026, 8, 21, 18, 0, tzinfo=timezone.utc)
DIGEST = "sha256:" + "1" * 64
OTHER_DIGEST = "sha256:" + "2" * 64
BASE_COMMIT = "a" * 40
BUILDER_COMMIT = "b" * 40
IDENTITY = SwarmIdentity(
    card_id="card-1",
    card_hash="sha256:" + "c" * 64,
    base_commit=BASE_COMMIT,
    evidence_id="sha256:" + "d" * 64,
    trajectory_id="trajectory-1",
)


@dataclass(frozen=True)
class Workflow:
    plan: SwarmPlan
    results: tuple[SubagentResult, ...]
    receipts: tuple[PhaseReceipt, ...]


def _plan() -> SwarmPlan:
    return SwarmPlan(
        plan_id="plan-1",
        identity=IDENTITY,
        phases=(
            SwarmPhaseSpec(
                phase_id="inspect", role=SwarmRole.SCOUT, contract_ids=("scout",)
            ),
            SwarmPhaseSpec(
                phase_id="build",
                role=SwarmRole.BUILDER,
                contract_ids=("builder",),
                predecessor_phase_ids=("inspect",),
            ),
            SwarmPhaseSpec(
                phase_id="test",
                role=SwarmRole.TESTER,
                contract_ids=("tester",),
                predecessor_phase_ids=("build",),
            ),
        ),
        created_at=NOW,
    )


def _contract(plan: SwarmPlan, contract_id: str, phase_id: str, role: SwarmRole):
    return SubagentContract(
        contract_id=contract_id,
        team_id="team-1",
        identity=plan.identity,
        plan_hash=plan.content_hash,
        phase_id=phase_id,
        parent_agent_id="orchestrator",
        child_agent_id=f"agent-{contract_id}",
        role=role,
        task=f"Perform only the {phase_id} phase.",
        readable_paths=("src/skharness/arena", "tests"),
        writable_paths=("src/skharness/arena",) if role is SwarmRole.BUILDER else (),
        protected_paths=("tests/hidden",),
        tool_allowlist=("rg", "pytest"),
        budget=ExecutionBudget(
            wall_seconds=60,
            token_limit=1_000,
            tool_call_limit=20,
            cost_limit=1,
        ),
        lease_id=f"lease-{contract_id}",
        worktree_id="worktree-1",
        issued_at=NOW,
    )


def _result(
    contract: SubagentContract,
    *,
    observed_commit: str | None = None,
    scout_assessment: ScoutAssessment | None = None,
    finished_at: datetime,
) -> SubagentResult:
    scout_findings = ()
    evidence_refs = (DIGEST,)
    if contract.role is SwarmRole.SCOUT:
        finding = ScoutFinding.create(
            path="src/skharness/arena/swarm.py",
            line=454,
            detail="The scout phase requires an actionable typed source finding.",
        )
        scout_findings = (finding,)
        evidence_refs = (DIGEST, finding.digest)
    return SubagentResult.from_contract(
        contract,
        disposition=SubagentDisposition.COMPLETED,
        summary=f"Structured {contract.phase_id} result; process exit alone is not authority.",
        evidence_refs=evidence_refs,
        observed_commit=observed_commit,
        scout_assessment=scout_assessment,
        scout_findings=scout_findings,
        started_at=finished_at - timedelta(seconds=1),
        finished_at=finished_at,
    )


def _workflow(*, builder_commit: str = BUILDER_COMMIT) -> Workflow:
    plan = _plan()
    scout_contract = _contract(plan, "scout", "inspect", SwarmRole.SCOUT)
    scout_result = _result(
        scout_contract,
        scout_assessment=ScoutAssessment.ACTIONABLE,
        finished_at=NOW + timedelta(seconds=1),
    )
    scout_receipt = PhaseReceipt.from_result(
        scout_contract,
        scout_result,
        recorded_at=NOW + timedelta(seconds=2),
    )

    builder_contract = bind_phase_inputs(
        _contract(plan, "builder", "build", SwarmRole.BUILDER),
        (scout_receipt,),
        plan=plan,
        bound_at=NOW + timedelta(seconds=3),
    )
    builder_result = _result(
        builder_contract,
        observed_commit=builder_commit,
        finished_at=NOW + timedelta(seconds=4),
    )
    builder_receipt = PhaseReceipt.from_result(
        builder_contract,
        builder_result,
        predecessors=(scout_receipt,),
        recorded_at=NOW + timedelta(seconds=5),
    )

    tester_contract = bind_phase_inputs(
        _contract(plan, "tester", "test", SwarmRole.TESTER),
        (builder_receipt,),
        plan=plan,
        bound_at=NOW + timedelta(seconds=6),
    )
    tester_result = _result(
        tester_contract,
        finished_at=NOW + timedelta(seconds=7),
    )
    tester_receipt = PhaseReceipt.from_result(
        tester_contract,
        tester_result,
        predecessors=(builder_receipt,),
        recorded_at=NOW + timedelta(seconds=8),
    )
    return Workflow(
        plan,
        (scout_result, builder_result, tester_result),
        (scout_receipt, builder_receipt, tester_receipt),
    )


def _evidence(
    criterion_id: str,
    *,
    passed: bool = True,
    observed_by: str = "agent-verifier",
    source: EvidenceSource = EvidenceSource.VERIFIER,
    provenance: CheckProvenance = CheckProvenance.PREEXISTING,
) -> CriterionEvidence:
    return CriterionEvidence(
        criterion_id=criterion_id,
        passed=passed,
        artifact_digest=OTHER_DIGEST,
        observed_by=observed_by,
        source=source,
        test_provenance=provenance,
    )


def _attestation(
    workflow: Workflow,
    *,
    results: tuple[SubagentResult, ...] | None = None,
    receipts: tuple[PhaseReceipt, ...] | None = None,
    verifier_agent_id: str = "agent-verifier",
    verdict: VerifierVerdict = VerifierVerdict.APPROVED,
    criteria: tuple[CriterionEvidence, ...] | None = None,
    signature: str = "trusted-signature",
) -> VerifierAttestation:
    results = workflow.results if results is None else results
    receipts = workflow.receipts if receipts is None else receipts
    return VerifierAttestation(
        identity=workflow.plan.identity,
        plan_hash=workflow.plan.content_hash,
        phase_lineage_digest=phase_lineage_digest(receipts),
        final_commit=receipts[-1].output_commit,
        verifier_agent_id=verifier_agent_id,
        verdict=verdict,
        subject_result_hashes=tuple(result.content_hash for result in results),
        criteria=(
            _evidence("focused-tests"),
            _evidence("acceptance", provenance=CheckProvenance.INDEPENDENT_RUNTIME),
        )
        if criteria is None
        else criteria,
        created_at=NOW + timedelta(seconds=9),
        signature=signature,
    )


def _gate(workflow: Workflow, *, signature_verifier=None) -> SwarmCompletionGate:
    return SwarmCompletionGate(
        plan=workflow.plan,
        required_criteria=("focused-tests", "acceptance"),
        trusted_verifier_ids=("agent-verifier",),
        verify_signature=signature_verifier
        or (lambda attestation: attestation.signature == "trusted-signature"),
    )


def test_exact_planned_lineage_and_final_commit_authorize_completion():
    workflow = _workflow()
    decision = _gate(workflow).evaluate(
        workflow.results, workflow.receipts, _attestation(workflow)
    )
    assert decision.authorized
    assert decision.reasons == ()
    assert decision.verifier_attestation_digest is not None


def test_old_unplanned_api_and_v1_attestation_fail_closed():
    workflow = _workflow()
    with pytest.raises(TypeError):
        SwarmCompletionGate(
            identity=IDENTITY,
            required_roles=(SwarmRole.BUILDER,),
            required_criteria=("acceptance",),
            trusted_verifier_ids=("agent-verifier",),
            verify_signature=lambda _: True,
        )
    with pytest.raises(ValidationError, match="schema_version|plan_hash"):
        VerifierAttestation.model_validate(
            {
                "schema_version": "arena.swarm-verifier.v1",
                "identity": IDENTITY,
                "verifier_agent_id": "agent-verifier",
                "verdict": "approved",
                "subject_result_hashes": tuple(
                    result.content_hash for result in workflow.results
                ),
                "criteria": (),
                "created_at": NOW,
                "signature": "legacy",
            }
        )


def test_worker_text_exit_zero_and_completed_results_cannot_self_complete():
    workflow = _workflow()
    decision = _gate(workflow).evaluate(workflow.results, workflow.receipts, None)
    assert not decision.authorized
    assert decision.reasons == ("verifier_attestation_missing",)


@pytest.mark.parametrize("kind", ("result", "receipt"))
def test_reordered_phase_material_is_rejected_even_when_complete(kind):
    workflow = _workflow()
    results = workflow.results
    receipts = workflow.receipts
    if kind == "result":
        results = (results[1], results[0], results[2])
    else:
        receipts = (receipts[1], receipts[0], receipts[2])
    decision = _gate(workflow).evaluate(
        results, receipts, _attestation(workflow, results=results, receipts=receipts)
    )
    assert not decision.authorized
    assert f"planned_{kind}_order_mismatch" in decision.reasons


def test_omitted_or_unplanned_worker_cannot_be_hidden_by_attestation():
    workflow = _workflow()
    omitted_results = workflow.results[:-1]
    omitted_receipts = workflow.receipts[:-1]
    omitted = _gate(workflow).evaluate(
        omitted_results,
        omitted_receipts,
        _attestation(workflow, results=omitted_results, receipts=omitted_receipts),
    )
    assert "planned_result_set_mismatch" in omitted.reasons
    assert "planned_receipt_set_mismatch" in omitted.reasons
    assert "terminal_receipt_missing" in omitted.reasons

    rogue_result = workflow.results[-1].model_copy(update={"contract_id": "rogue"})
    rogue_receipt = workflow.receipts[-1].model_copy(update={"contract_id": "rogue"})
    results = (*workflow.results, rogue_result)
    receipts = (*workflow.receipts, rogue_receipt)
    unplanned = _gate(workflow).evaluate(
        results, receipts, _attestation(workflow, results=results, receipts=receipts)
    )
    assert "planned_result_set_mismatch" in unplanned.reasons
    assert "planned_receipt_set_mismatch" in unplanned.reasons


def test_interrupted_lineage_retains_blocking_scout_evidence_without_receipt():
    workflow = _workflow()
    blocked_scout = workflow.results[0].model_copy(
        update={
            "disposition": SubagentDisposition.BLOCKED,
            "scout_assessment": ScoutAssessment.BLOCKED,
            "scout_findings": (),
            "reason_codes": ("missing_dependency",),
            "evidence_refs": (),
        }
    )
    decision = _gate(workflow).evaluate((blocked_scout,), (), None)
    assert "worker_not_completed" in decision.reasons
    assert "worker_not_completed:scout" in decision.reasons
    assert "scout_not_actionable:scout" in decision.reasons
    assert "verifier_attestation_missing" in decision.reasons


def test_stale_builder_commit_and_tester_commit_mutation_are_rejected():
    stale = _workflow(builder_commit=BASE_COMMIT)
    stale_decision = _gate(stale).evaluate(
        stale.results, stale.receipts, _attestation(stale)
    )
    assert "builder_stale_commit:builder" in stale_decision.reasons

    workflow = _workflow()
    mutated_tester = workflow.receipts[-1].model_copy(update={"output_commit": "c" * 40})
    receipts = (*workflow.receipts[:-1], mutated_tester)
    mutated = _gate(workflow).evaluate(
        workflow.results,
        receipts,
        _attestation(workflow, receipts=receipts),
    )
    assert "read_only_phase_commit_changed:tester" in mutated.reasons
    assert "receipt_output_commit_mismatch:tester" in mutated.reasons


def test_controller_receipt_and_predecessor_lineage_cannot_be_substituted():
    workflow = _workflow()
    changed_result = workflow.results[1].model_copy(update={"summary": "substituted"})
    results = (workflow.results[0], changed_result, workflow.results[2])
    substituted = _gate(workflow).evaluate(
        results,
        workflow.receipts,
        _attestation(workflow, results=results),
    )
    assert "controller_receipt_mismatch:builder" in substituted.reasons

    broken_tester = workflow.receipts[-1].model_copy(
        update={"predecessor_receipt_hashes": (OTHER_DIGEST,)}
    )
    receipts = (*workflow.receipts[:-1], broken_tester)
    broken = _gate(workflow).evaluate(
        workflow.results,
        receipts,
        _attestation(workflow, receipts=receipts),
    )
    assert "phase_lineage_mismatch:tester" in broken.reasons


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("plan_hash", OTHER_DIGEST, "verifier_plan_mismatch"),
        ("phase_lineage_digest", OTHER_DIGEST, "verifier_lineage_mismatch"),
        ("final_commit", "c" * 40, "verifier_final_commit_mismatch"),
        ("subject_result_hashes", (), "verifier_subject_mismatch"),
    ],
)
def test_attestation_must_bind_exact_plan_lineage_commit_and_results(field, value, reason):
    workflow = _workflow()
    attestation = _attestation(workflow).model_copy(update={field: value})
    decision = _gate(workflow).evaluate(workflow.results, workflow.receipts, attestation)
    assert not decision.authorized
    assert reason in decision.reasons


@pytest.mark.parametrize(
    ("provenance", "source", "reason"),
    [
        (
            CheckProvenance.WORKER_AUTHORED,
            EvidenceSource.VERIFIER,
            "criterion_worker_authored_test:focused-tests",
        ),
        (
            CheckProvenance.PREEXISTING,
            EvidenceSource.WORKER,
            "criterion_not_verifier_observed:focused-tests",
        ),
    ],
)
def test_worker_authored_or_worker_observed_checks_cannot_authorize(
    provenance, source, reason
):
    workflow = _workflow()
    attestation = _attestation(
        workflow,
        criteria=(
            _evidence("focused-tests", provenance=provenance, source=source),
            _evidence("acceptance"),
        ),
    )
    decision = _gate(workflow).evaluate(workflow.results, workflow.receipts, attestation)
    assert not decision.authorized
    assert reason in decision.reasons


def test_verifier_must_be_trusted_signed_and_independent():
    workflow = _workflow()
    results = (
        workflow.results[0].model_copy(update={"agent_id": "agent-verifier"}),
        *workflow.results[1:],
    )
    attestation = _attestation(workflow, results=results, signature="forged")
    decision = _gate(workflow).evaluate(results, workflow.receipts, attestation)
    assert "verifier_not_independent" in decision.reasons
    assert "verifier_signature_invalid" in decision.reasons


@pytest.mark.parametrize(
    "verdict", (VerifierVerdict.REJECTED, VerifierVerdict.INCONCLUSIVE)
)
def test_non_approved_verifier_dispositions_fail_closed(verdict):
    workflow = _workflow()
    decision = _gate(workflow).evaluate(
        workflow.results,
        workflow.receipts,
        _attestation(workflow, verdict=verdict),
    )
    assert f"verifier_{verdict.value}" in decision.reasons


def test_signature_verification_infrastructure_error_fails_closed():
    workflow = _workflow()

    def unavailable(_attestation):
        raise TimeoutError("signing trust store unavailable")

    decision = _gate(workflow, signature_verifier=unavailable).evaluate(
        workflow.results, workflow.receipts, _attestation(workflow)
    )
    assert "verifier_signature_invalid" in decision.reasons
