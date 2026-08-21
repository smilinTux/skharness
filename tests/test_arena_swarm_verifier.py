"""Independent completion authority for multi-agent Arena runs."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from skharness.arena.swarm import (
    SubagentDisposition,
    SubagentResult,
    SwarmIdentity,
    SwarmRole,
)
from skharness.arena.swarm_verifier import (
    CheckProvenance,
    CriterionEvidence,
    EvidenceSource,
    SwarmCompletionGate,
    VerifierAttestation,
    VerifierVerdict,
)

NOW = datetime(2026, 8, 21, 18, 0, tzinfo=timezone.utc)
IDENTITY = SwarmIdentity(
    card_id="card-1",
    card_hash="sha256:" + "c" * 64,
    base_commit="a" * 40,
    evidence_id="sha256:" + "b" * 64,
    trajectory_id="trajectory-1",
)


def _result(
    role: SwarmRole,
    *,
    agent_id: str | None = None,
    disposition: SubagentDisposition = SubagentDisposition.COMPLETED,
) -> SubagentResult:
    completed = disposition is SubagentDisposition.COMPLETED
    return SubagentResult(
        contract_id=f"contract-{role.value}",
        contract_hash="sha256:" + role.value.encode().hex().ljust(64, "0")[:64],
        identity=IDENTITY,
        agent_id=agent_id or f"agent-{role.value}",
        role=role,
        disposition=disposition,
        summary="exit_code=0; worker says every acceptance criterion passed",
        reason_codes=() if completed else (f"worker_{disposition.value}",),
        evidence_refs=("sha256:" + "c" * 64,) if completed else (),
        observed_commit="d" * 40 if role is SwarmRole.BUILDER and completed else None,
        started_at=NOW,
        finished_at=NOW,
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
        artifact_digest="sha256:" + "e" * 64,
        observed_by=observed_by,
        source=source,
        test_provenance=provenance,
    )


def _attestation(
    results: tuple[SubagentResult, ...],
    *,
    verifier_agent_id: str = "agent-verifier",
    verdict: VerifierVerdict = VerifierVerdict.APPROVED,
    criteria: tuple[CriterionEvidence, ...] | None = None,
    signature: str = "trusted-signature",
) -> VerifierAttestation:
    return VerifierAttestation(
        identity=IDENTITY,
        verifier_agent_id=verifier_agent_id,
        verdict=verdict,
        subject_result_hashes=tuple(result.content_hash for result in results),
        criteria=criteria
        or (
            _evidence("focused-tests"),
            _evidence("acceptance", provenance=CheckProvenance.INDEPENDENT_RUNTIME),
        ),
        created_at=NOW,
        signature=signature,
    )


def _gate(*, signature_verifier=None) -> SwarmCompletionGate:
    return SwarmCompletionGate(
        identity=IDENTITY,
        required_roles=(SwarmRole.BUILDER, SwarmRole.TESTER),
        required_criteria=("focused-tests", "acceptance"),
        trusted_verifier_ids=("agent-verifier",),
        verify_signature=signature_verifier
        or (lambda attestation: attestation.signature == "trusted-signature"),
    )


def test_independent_signed_verifier_evidence_is_the_only_positive_completion_path():
    results = (_result(SwarmRole.BUILDER), _result(SwarmRole.TESTER))

    decision = _gate().evaluate(results, _attestation(results))

    assert decision.authorized
    assert decision.reasons == ()
    assert decision.verifier_attestation_digest is not None


def test_worker_text_exit_zero_and_completed_disposition_cannot_self_complete():
    results = (_result(SwarmRole.BUILDER), _result(SwarmRole.TESTER))

    decision = _gate().evaluate(results, None)

    assert not decision.authorized
    assert decision.reasons == ("verifier_attestation_missing",)
    assert decision.verifier_attestation_digest is None


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
    results = (_result(SwarmRole.BUILDER), _result(SwarmRole.TESTER))
    evidence = (
        _evidence("focused-tests", provenance=provenance, source=source),
        _evidence("acceptance"),
    )

    decision = _gate().evaluate(results, _attestation(results, criteria=evidence))

    assert not decision.authorized
    assert reason in decision.reasons


def test_verifier_must_be_trusted_signed_and_independent_of_workers():
    results = (
        _result(SwarmRole.BUILDER, agent_id="agent-verifier"),
        _result(SwarmRole.TESTER),
    )
    attestation = _attestation(results, signature="forged")

    decision = _gate().evaluate(results, attestation)

    assert not decision.authorized
    assert "verifier_not_independent" in decision.reasons
    assert "verifier_signature_invalid" in decision.reasons


@pytest.mark.parametrize(
    "verdict", (VerifierVerdict.REJECTED, VerifierVerdict.INCONCLUSIVE)
)
def test_non_approved_verifier_dispositions_fail_closed(verdict):
    results = (_result(SwarmRole.BUILDER), _result(SwarmRole.TESTER))

    decision = _gate().evaluate(results, _attestation(results, verdict=verdict))

    assert not decision.authorized
    assert f"verifier_{verdict.value}" in decision.reasons


def test_attestation_must_cover_exact_immutable_worker_result_set():
    results = (_result(SwarmRole.BUILDER), _result(SwarmRole.TESTER))
    attestation = _attestation(results).model_copy(
        update={"subject_result_hashes": (results[0].content_hash,)}
    )

    decision = _gate().evaluate(results, attestation)

    assert not decision.authorized
    assert "verifier_subject_mismatch" in decision.reasons


def test_card_commit_evidence_and_trajectory_identity_must_match_every_record():
    results = (_result(SwarmRole.BUILDER), _result(SwarmRole.TESTER))
    other_identity = IDENTITY.model_copy(update={"base_commit": "f" * 40})
    mismatched_results = (results[0].model_copy(update={"identity": other_identity}), results[1])
    mismatched_attestation = _attestation(mismatched_results).model_copy(
        update={"identity": other_identity}
    )

    decision = _gate().evaluate(mismatched_results, mismatched_attestation)

    assert not decision.authorized
    assert "worker_identity_mismatch" in decision.reasons
    assert "verifier_identity_mismatch" in decision.reasons


def test_every_required_acceptance_criterion_must_exist_and_pass():
    results = (_result(SwarmRole.BUILDER), _result(SwarmRole.TESTER))
    attestation = _attestation(
        results,
        criteria=(_evidence("focused-tests", passed=False),),
    )

    decision = _gate().evaluate(results, attestation)

    assert not decision.authorized
    assert "criterion_failed:focused-tests" in decision.reasons
    assert "criterion_missing:acceptance" in decision.reasons


def test_negative_worker_disposition_cannot_be_overridden_by_approval():
    results = (
        _result(SwarmRole.BUILDER),
        _result(SwarmRole.TESTER, disposition=SubagentDisposition.BLOCKED),
    )

    decision = _gate().evaluate(results, _attestation(results))

    assert not decision.authorized
    assert "worker_not_completed" in decision.reasons
    assert "missing_completed_role:tester" in decision.reasons


def test_signature_verification_infrastructure_error_fails_closed():
    results = (_result(SwarmRole.BUILDER), _result(SwarmRole.TESTER))

    def unavailable(_attestation):
        raise TimeoutError("signing trust store unavailable")

    decision = _gate(signature_verifier=unavailable).evaluate(results, _attestation(results))

    assert not decision.authorized
    assert "verifier_signature_invalid" in decision.reasons
