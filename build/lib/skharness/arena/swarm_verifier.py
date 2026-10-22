"""Fail-closed completion authority for an exact Arena swarm phase lineage.

Worker results and normal process exits are never completion authority. The gate
derives required workers from an immutable :class:`SwarmPlan`, checks controller
receipts in canonical plan order, and requires a trusted independent verifier to
sign that exact lineage and final commit.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from .models import FrozenModel, canonical_digest
from .swarm import (
    PhaseReceipt,
    ScoutAssessment,
    SubagentDisposition,
    SubagentResult,
    SwarmIdentity,
    SwarmPlan,
    SwarmRole,
)

_COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class VerifierVerdict(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


class EvidenceSource(str, Enum):
    VERIFIER = "verifier"
    WORKER = "worker"


class CheckProvenance(str, Enum):
    PREEXISTING = "preexisting"
    VERIFIER_AUTHORED = "verifier_authored"
    INDEPENDENT_RUNTIME = "independent_runtime"
    WORKER_AUTHORED = "worker_authored"


class CriterionEvidence(FrozenModel):
    criterion_id: str
    passed: bool
    artifact_digest: str
    observed_by: str
    source: EvidenceSource
    test_provenance: CheckProvenance

    @model_validator(mode="after")
    def validate_identity(self) -> CriterionEvidence:
        values = (self.criterion_id, self.artifact_digest, self.observed_by)
        if any(not value.strip() for value in values):
            raise ValueError("criterion evidence identity fields must not be blank")
        if not _DIGEST_RE.fullmatch(self.artifact_digest):
            raise ValueError("criterion artifact must be an immutable sha256 digest")
        return self


def phase_lineage_digest(receipts: Sequence[PhaseReceipt]) -> str:
    """Hash receipt identities in canonical plan order supplied by the caller."""

    # JSON encodes this tuple as an ordered array. The gate separately proves
    # that the supplied order exactly matches the immutable plan.
    return canonical_digest(tuple(receipt.content_hash for receipt in receipts))  # type: ignore[arg-type]


class VerifierAttestation(FrozenModel):
    """Signed verifier decision over one exact plan, lineage, and final commit."""

    schema_version: Literal["arena.swarm-verifier.v2"] = "arena.swarm-verifier.v2"
    identity: SwarmIdentity
    plan_hash: str
    phase_lineage_digest: str
    final_commit: str
    verifier_agent_id: str
    verdict: VerifierVerdict
    subject_result_hashes: tuple[str, ...]
    criteria: tuple[CriterionEvidence, ...]
    created_at: datetime
    signature: str = Field(repr=False)

    @model_validator(mode="after")
    def validate_attestation(self) -> VerifierAttestation:
        if not self.verifier_agent_id.strip() or not self.signature.strip():
            raise ValueError("verifier identity and signature are required")
        for name in ("plan_hash", "phase_lineage_digest"):
            if not _DIGEST_RE.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} must be an immutable sha256 digest")
        if not _COMMIT_RE.fullmatch(self.final_commit):
            raise ValueError("final_commit must be a full Git object ID")
        if len(self.subject_result_hashes) != len(set(self.subject_result_hashes)):
            raise ValueError("subject result hashes must be unique")
        if any(not _DIGEST_RE.fullmatch(value) for value in self.subject_result_hashes):
            raise ValueError("subject result hashes must be immutable sha256 digests")
        criterion_ids = [evidence.criterion_id for evidence in self.criteria]
        if len(criterion_ids) != len(set(criterion_ids)):
            raise ValueError("criterion evidence must be unique")
        return self

    @property
    def signing_digest(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude={"signature"}))


class CompletionDecision(FrozenModel):
    authorized: bool
    reasons: tuple[str, ...]
    verifier_attestation_digest: str | None = None


AttestationSignatureVerifier = Callable[[VerifierAttestation], bool]


class SwarmCompletionGate:
    """Authorize only a complete, ordered, commit-consistent planned lineage."""

    def __init__(
        self,
        *,
        plan: SwarmPlan,
        required_criteria: Iterable[str],
        trusted_verifier_ids: Iterable[str],
        verify_signature: AttestationSignatureVerifier,
    ) -> None:
        self.plan = plan
        self.required_criteria = frozenset(required_criteria)
        self.trusted_verifier_ids = frozenset(trusted_verifier_ids)
        self.verify_signature = verify_signature
        if not self.required_criteria or any(
            not criterion.strip() for criterion in self.required_criteria
        ):
            raise ValueError("non-empty acceptance criteria are required")
        if not self.trusted_verifier_ids or any(
            not verifier.strip() for verifier in self.trusted_verifier_ids
        ):
            raise ValueError("non-empty trusted verifier identities are required")

    def evaluate(
        self,
        results: Iterable[SubagentResult],
        receipts: Iterable[PhaseReceipt],
        attestation: VerifierAttestation | None,
    ) -> CompletionDecision:
        results = tuple(results)
        receipts = tuple(receipts)
        reasons: set[str] = set()
        planned = tuple(
            (phase.phase_id, phase.role, contract_id)
            for phase in self.plan.phases
            for contract_id in phase.contract_ids
        )
        expected_ids = tuple(item[2] for item in planned)
        self._check_exact_order(
            "result", tuple(result.contract_id for result in results), expected_ids, reasons
        )
        self._check_exact_order(
            "receipt", tuple(receipt.contract_id for receipt in receipts), expected_ids, reasons
        )

        results_by_id = {result.contract_id: result for result in results}
        receipts_by_id = {receipt.contract_id: receipt for receipt in receipts}
        canonical_results = tuple(
            results_by_id[item] for item in expected_ids if item in results_by_id
        )
        canonical_receipts = tuple(
            receipts_by_id[item] for item in expected_ids if item in receipts_by_id
        )
        phase_by_id = {phase.phase_id: phase for phase in self.plan.phases}
        planned_by_contract = {
            contract_id: (phase_id, role)
            for phase_id, role, contract_id in planned
        }

        # Preserve negative task evidence even when interruption prevented the
        # controller from issuing a receipt for that worker.
        for result in results:
            expected_phase = planned_by_contract.get(result.contract_id)
            if result.identity != self.plan.identity:
                reasons.add(f"worker_identity_mismatch:{result.contract_id}")
            if result.plan_hash != self.plan.content_hash:
                reasons.add(f"worker_plan_mismatch:{result.contract_id}")
            if expected_phase is not None and (
                result.phase_id != expected_phase[0] or result.role is not expected_phase[1]
            ):
                reasons.add(f"worker_phase_mismatch:{result.contract_id}")
            if result.disposition is not SubagentDisposition.COMPLETED:
                reasons.add("worker_not_completed")
                reasons.add(f"worker_not_completed:{result.contract_id}")
            if not result.evidence_refs:
                reasons.add(f"worker_evidence_missing:{result.contract_id}")
            if (
                result.role is SwarmRole.SCOUT
                and result.scout_assessment is not ScoutAssessment.ACTIONABLE
            ):
                reasons.add(f"scout_not_actionable:{result.contract_id}")

        for phase_id, role, contract_id in planned:
            result = results_by_id.get(contract_id)
            receipt = receipts_by_id.get(contract_id)
            if result is None or receipt is None:
                continue
            self._check_receipt_binding(result, receipt, phase_id, role, reasons)
            phase = phase_by_id[phase_id]
            predecessor_ids = tuple(
                predecessor_contract_id
                for predecessor_phase_id in phase.predecessor_phase_ids
                for predecessor_contract_id in phase_by_id[
                    predecessor_phase_id
                ].contract_ids
            )
            predecessor_receipts = tuple(
                receipts_by_id[item] for item in predecessor_ids if item in receipts_by_id
            )
            if len(predecessor_receipts) != len(predecessor_ids):
                reasons.add(f"phase_predecessor_missing:{contract_id}")
            else:
                expected_predecessors = tuple(item.content_hash for item in predecessor_receipts)
                if receipt.predecessor_receipt_hashes != expected_predecessors:
                    reasons.add(f"phase_lineage_mismatch:{contract_id}")
            self._check_commit_chain(
                receipt, role, predecessor_receipts, predecessor_ids, reasons
            )

        predecessor_phase_ids = {
            predecessor
            for phase in self.plan.phases
            for predecessor in phase.predecessor_phase_ids
        }
        terminal_ids = tuple(
            contract_id
            for phase in self.plan.phases
            if phase.phase_id not in predecessor_phase_ids
            for contract_id in phase.contract_ids
        )
        terminal_receipts = tuple(
            receipts_by_id[item] for item in terminal_ids if item in receipts_by_id
        )
        final_commits = {receipt.output_commit for receipt in terminal_receipts}
        final_commit = next(iter(final_commits)) if len(final_commits) == 1 else None
        if len(terminal_receipts) != len(terminal_ids):
            reasons.add("terminal_receipt_missing")
        if len(final_commits) != 1:
            reasons.add("terminal_commit_conflict")

        if attestation is None:
            reasons.add("verifier_attestation_missing")
            return self._decision(reasons)
        if attestation.identity != self.plan.identity:
            reasons.add("verifier_identity_mismatch")
        if attestation.plan_hash != self.plan.content_hash:
            reasons.add("verifier_plan_mismatch")
        calculated_lineage = phase_lineage_digest(canonical_receipts)
        if (
            len(canonical_receipts) != len(expected_ids)
            or attestation.phase_lineage_digest != calculated_lineage
        ):
            reasons.add("verifier_lineage_mismatch")
        if final_commit is None or attestation.final_commit != final_commit:
            reasons.add("verifier_final_commit_mismatch")
        expected_result_hashes = tuple(result.content_hash for result in canonical_results)
        if (
            len(canonical_results) != len(expected_ids)
            or attestation.subject_result_hashes != expected_result_hashes
        ):
            reasons.add("verifier_subject_mismatch")
        if attestation.verifier_agent_id not in self.trusted_verifier_ids:
            reasons.add("verifier_untrusted")
        if attestation.verifier_agent_id in {result.agent_id for result in results}:
            reasons.add("verifier_not_independent")
        try:
            signature_valid = self.verify_signature(attestation)
        except Exception:
            signature_valid = False
        if not signature_valid:
            reasons.add("verifier_signature_invalid")
        if attestation.verdict is not VerifierVerdict.APPROVED:
            reasons.add(f"verifier_{attestation.verdict.value}")

        evidence_by_id = {evidence.criterion_id: evidence for evidence in attestation.criteria}
        for criterion_id in sorted(self.required_criteria):
            evidence = evidence_by_id.get(criterion_id)
            if evidence is None:
                reasons.add(f"criterion_missing:{criterion_id}")
                continue
            if not evidence.passed:
                reasons.add(f"criterion_failed:{criterion_id}")
            if (
                evidence.source is not EvidenceSource.VERIFIER
                or evidence.observed_by != attestation.verifier_agent_id
            ):
                reasons.add(f"criterion_not_verifier_observed:{criterion_id}")
            if evidence.test_provenance is CheckProvenance.WORKER_AUTHORED:
                reasons.add(f"criterion_worker_authored_test:{criterion_id}")
        return self._decision(reasons, attestation)

    def _check_receipt_binding(
        self,
        result: SubagentResult,
        receipt: PhaseReceipt,
        phase_id: str,
        role: SwarmRole,
        reasons: set[str],
    ) -> None:
        contract_id = result.contract_id
        expected = (
            self.plan.content_hash,
            self.plan.identity.content_hash,
            phase_id,
            result.contract_id,
            result.contract_hash,
            result.lease_id,
            role,
            result.content_hash,
            result.disposition,
            result.scout_assessment,
            result.evidence_refs,
            result.finished_at,
        )
        actual = (
            receipt.plan_hash,
            receipt.identity_hash,
            receipt.phase_id,
            receipt.contract_id,
            receipt.contract_hash,
            receipt.lease_id,
            receipt.role,
            receipt.result_hash,
            receipt.disposition,
            receipt.scout_assessment,
            receipt.evidence_refs,
            receipt.result_finished_at,
        )
        if actual != expected:
            reasons.add(f"controller_receipt_mismatch:{contract_id}")
        expected_output = result.observed_commit or receipt.input_commit
        if receipt.output_commit != expected_output:
            reasons.add(f"receipt_output_commit_mismatch:{contract_id}")

    def _check_commit_chain(
        self,
        receipt: PhaseReceipt,
        role: SwarmRole,
        predecessors: tuple[PhaseReceipt, ...],
        predecessor_ids: tuple[str, ...],
        reasons: set[str],
    ) -> None:
        contract_id = receipt.contract_id
        if predecessor_ids:
            commits = {item.output_commit for item in predecessors}
            if len(commits) != 1:
                reasons.add(f"predecessor_commit_conflict:{contract_id}")
            elif receipt.input_commit != next(iter(commits)):
                reasons.add(f"phase_input_commit_mismatch:{contract_id}")
        elif receipt.input_commit != self.plan.identity.base_commit:
            reasons.add(f"root_input_commit_mismatch:{contract_id}")
        if role in {SwarmRole.SCOUT, SwarmRole.TESTER}:
            if receipt.output_commit != receipt.input_commit:
                reasons.add(f"read_only_phase_commit_changed:{contract_id}")
        elif role is SwarmRole.BUILDER and receipt.output_commit == receipt.input_commit:
            reasons.add(f"builder_stale_commit:{contract_id}")

    @staticmethod
    def _check_exact_order(
        label: str,
        actual: tuple[str, ...],
        expected: tuple[str, ...],
        reasons: set[str],
    ) -> None:
        if actual == expected:
            return
        if len(actual) == len(expected) and set(actual) == set(expected):
            reasons.add(f"planned_{label}_order_mismatch")
        else:
            reasons.add(f"planned_{label}_set_mismatch")

    @staticmethod
    def _decision(
        reasons: set[str], attestation: VerifierAttestation | None = None
    ) -> CompletionDecision:
        return CompletionDecision(
            authorized=not reasons,
            reasons=tuple(sorted(reasons)),
            verifier_attestation_digest=(
                canonical_digest(attestation) if attestation is not None and not reasons else None
            ),
        )
