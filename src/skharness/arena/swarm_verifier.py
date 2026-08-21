"""Fail-closed completion authority for orchestrated agent teams.

Subagents can report what they did, and process supervisors can report whether a
process exited normally.  Neither is completion authority.  This module grants
that authority only when a separately trusted verifier signs evidence covering
the exact immutable swarm identity and every contributing result.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from .models import FrozenModel, canonical_digest
from .swarm import SubagentDisposition, SubagentResult, SwarmIdentity, SwarmRole

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class VerifierVerdict(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


class EvidenceSource(str, Enum):
    """Who directly observed an acceptance-criterion result."""

    VERIFIER = "verifier"
    WORKER = "worker"


class CheckProvenance(str, Enum):
    """Origin of the check used as evidence.

    Worker-authored checks may remain useful artifacts, but are never sufficient
    evidence for a positive completion decision.
    """

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


class VerifierAttestation(FrozenModel):
    """Verifier-owned, signed decision over exact role-result hashes."""

    schema_version: Literal["arena.swarm-verifier.v1"] = "arena.swarm-verifier.v1"
    identity: SwarmIdentity
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
        """Digest covered by the external verifier signature."""

        return canonical_digest(self.model_dump(mode="json", exclude={"signature"}))


class CompletionDecision(FrozenModel):
    authorized: bool
    reasons: tuple[str, ...]
    verifier_attestation_digest: str | None = None


AttestationSignatureVerifier = Callable[[VerifierAttestation], bool]


class SwarmCompletionGate:
    """Authorize completion only from independent, trusted verifier evidence."""

    def __init__(
        self,
        *,
        identity: SwarmIdentity,
        required_roles: Iterable[SwarmRole],
        required_criteria: Iterable[str],
        trusted_verifier_ids: Iterable[str],
        verify_signature: AttestationSignatureVerifier,
    ) -> None:
        self.identity = identity
        self.required_roles = frozenset(required_roles)
        self.required_criteria = frozenset(required_criteria)
        self.trusted_verifier_ids = frozenset(trusted_verifier_ids)
        self.verify_signature = verify_signature
        forbidden = self.required_roles & {SwarmRole.ORCHESTRATOR, SwarmRole.VERIFIER}
        if forbidden:
            raise ValueError("orchestrator and verifier are authorities, not worker requirements")
        if not self.required_roles:
            raise ValueError("at least one worker role is required")
        if not self.required_criteria:
            raise ValueError("at least one acceptance criterion is required")
        if any(not criterion.strip() for criterion in self.required_criteria):
            raise ValueError("acceptance criteria must not be blank")
        if not self.trusted_verifier_ids:
            raise ValueError("at least one trusted verifier is required")
        if any(not verifier.strip() for verifier in self.trusted_verifier_ids):
            raise ValueError("trusted verifier identities must not be blank")

    def evaluate(
        self,
        results: Iterable[SubagentResult],
        attestation: VerifierAttestation | None,
    ) -> CompletionDecision:
        results = tuple(results)
        reasons: set[str] = set()

        if not results:
            reasons.add("missing_worker_results")
        if any(result.identity != self.identity for result in results):
            reasons.add("worker_identity_mismatch")
        if any(result.role in {SwarmRole.ORCHESTRATOR, SwarmRole.VERIFIER} for result in results):
            reasons.add("authority_role_in_worker_results")
        if any(result.disposition is not SubagentDisposition.COMPLETED for result in results):
            reasons.add("worker_not_completed")
        completed_roles = {
            result.role
            for result in results
            if result.disposition is SubagentDisposition.COMPLETED
        }
        for role in sorted(self.required_roles - completed_roles, key=lambda value: value.value):
            reasons.add(f"missing_completed_role:{role.value}")
        if any(not result.evidence_refs for result in results):
            reasons.add("worker_evidence_missing")

        if attestation is None:
            reasons.add("verifier_attestation_missing")
            return self._decision(reasons)
        if attestation.identity != self.identity:
            reasons.add("verifier_identity_mismatch")
        if attestation.verifier_agent_id not in self.trusted_verifier_ids:
            reasons.add("verifier_untrusted")
        worker_ids = {result.agent_id for result in results}
        if attestation.verifier_agent_id in worker_ids:
            reasons.add("verifier_not_independent")
        try:
            signature_valid = self.verify_signature(attestation)
        except Exception:  # verifier infrastructure failure must fail closed
            signature_valid = False
        if not signature_valid:
            reasons.add("verifier_signature_invalid")
        if attestation.verdict is not VerifierVerdict.APPROVED:
            reasons.add(f"verifier_{attestation.verdict.value}")

        expected_hashes = {canonical_digest(result) for result in results}
        if set(attestation.subject_result_hashes) != expected_hashes:
            reasons.add("verifier_subject_mismatch")

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
