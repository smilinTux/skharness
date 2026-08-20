"""Agent collaboration and fail-closed SKMemory refinement controls.

The collaboration catalog is a rebuildable view over immutable arena objects.  The
refinement journal is different: it is an append-only authorization record.  It never
writes SKMemory itself and therefore cannot accidentally turn a benchmark result into
durable memory authority.  A privileged integration consumes an explicitly authorized
proposal, performs an idempotent canary/promotion/rollback, and records its receipt.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import Field, model_validator

from .models import (
    Experiment,
    FrozenModel,
    Provenance,
    Result,
    VerificationState,
    canonical_digest,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - production images are POSIX
    fcntl = None  # type: ignore[assignment]


class CollaborationError(RuntimeError):
    """A collaboration request conflicts with immutable arena evidence."""


@dataclass(frozen=True)
class ExperimentMatch:
    experiment: Experiment
    result: Result | None


class ExperimentCatalog:
    """Deterministic discovery and branch construction over immutable records."""

    def __init__(self, experiments: Iterable[Experiment], results: Iterable[Result] = ()):
        self._experiments: dict[str, Experiment] = {}
        for experiment in experiments:
            previous = self._experiments.get(experiment.id)
            if previous is not None and previous.content_hash != experiment.content_hash:
                raise CollaborationError(f"conflicting experiment identity: {experiment.id}")
            self._experiments[experiment.id] = experiment
        self._results: dict[str, Result] = {}
        for result in results:
            previous = self._results.get(result.experiment_id)
            if previous is not None and previous.content_hash != result.content_hash:
                raise CollaborationError(
                    f"conflicting result identity: {result.experiment_id}"
                )
            experiment = self._experiments.get(result.experiment_id)
            if experiment is None or experiment.content_hash != result.experiment_hash:
                raise CollaborationError("result is not bound to the catalog experiment")
            if experiment.challenge_hash != result.challenge_hash:
                raise CollaborationError("result challenge does not match experiment")
            self._results[result.experiment_id] = result

    def discover(
        self,
        *,
        challenge_hash: str | None = None,
        actor: str | None = None,
        harness: str | None = None,
        verification: VerificationState | None = None,
    ) -> tuple[ExperimentMatch, ...]:
        matches: list[ExperimentMatch] = []
        for experiment in self._experiments.values():
            result = self._results.get(experiment.id)
            if challenge_hash is not None and experiment.challenge_hash != challenge_hash:
                continue
            if actor is not None and experiment.actor != actor:
                continue
            if harness is not None and experiment.harness != harness:
                continue
            if verification is not None and (
                result is None or result.verification is not verification
            ):
                continue
            matches.append(ExperimentMatch(experiment, result))
        return tuple(sorted(matches, key=lambda item: item.experiment.id))

    def positive_evidence(self) -> tuple["PositiveEvidence", ...]:
        """Return only independently verified-valid results as reproducible evidence."""

        return tuple(
            PositiveEvidence.from_result(self._experiments[experiment_id], result)
            for experiment_id, result in sorted(self._results.items())
            if result.verification is VerificationState.VALID
        )

    def reproduce_evidence(
        self,
        immutable_evidence_id: str,
        **new_experiment: Any,
    ) -> Experiment:
        matches = [
            record for record in self.positive_evidence()
            if record.evidence_id == immutable_evidence_id
        ]
        if len(matches) != 1:
            raise CollaborationError("unknown or non-valid immutable evidence ID")
        return self.reproduce(matches[0].experiment_id, **new_experiment)

    def reproduce(
        self,
        source_id: str,
        *,
        experiment_id: str,
        actor: str,
        run_id: str,
        created_at: datetime,
        attempt: int = 1,
    ) -> Experiment:
        source = self._source(source_id, experiment_id)
        return Experiment.model_validate(
            source.model_dump() | {
                "id": experiment_id,
                "attempt": attempt,
                "parent_id": None,
                "reproduces_id": source.id,
                "changed_dimensions": (),
                "actor": actor,
                "run_id": run_id,
                "repository_result_sha": None,
                "served_model": None,
                "gateway_request_id": None,
                "gateway_backend_id": None,
                "hardware_telemetry": {},
                "created_at": created_at,
                "artifacts": (),
            }
        )

    def mutate(
        self,
        parent_id: str,
        *,
        experiment_id: str,
        actor: str,
        run_id: str,
        changed_dimensions: Iterable[str],
        configuration: Mapping[str, Any],
        created_at: datetime,
        attempt: int = 1,
    ) -> Experiment:
        parent = self._source(parent_id, experiment_id)
        dimensions = tuple(sorted(set(changed_dimensions)))
        if not dimensions or any(not value.strip() for value in dimensions):
            raise CollaborationError("mutation requires named changed dimensions")
        if dict(configuration) == parent.configuration:
            raise CollaborationError("mutation configuration must differ from its parent")
        return Experiment.model_validate(
            parent.model_dump() | {
                "id": experiment_id,
                "attempt": attempt,
                "parent_id": parent.id,
                "reproduces_id": None,
                "changed_dimensions": dimensions,
                "actor": actor,
                "run_id": run_id,
                "configuration": dict(configuration),
                "repository_result_sha": None,
                "served_model": None,
                "gateway_request_id": None,
                "gateway_backend_id": None,
                "hardware_telemetry": {},
                "created_at": created_at,
                "artifacts": (),
            }
        )

    def _source(self, source_id: str, new_id: str) -> Experiment:
        if new_id in self._experiments:
            raise CollaborationError(f"experiment id already exists: {new_id}")
        try:
            return self._experiments[source_id]
        except KeyError as exc:
            raise CollaborationError(f"unknown source experiment: {source_id}") from exc


class NegativeKind(str, Enum):
    INVALID = "invalid"
    INCONCLUSIVE = "inconclusive"
    FAILED = "failed"


class PositiveEvidence(FrozenModel):
    """Content-addressed credit record for one verified-valid experiment result."""

    schema_version: str = "arena.positive.v1"
    evidence_id: str
    experiment_id: str
    experiment_hash: str
    result_hash: str
    challenge_hash: str
    actor: str
    parent_id: str | None = None
    reproduces_id: str | None = None

    @classmethod
    def from_result(cls, experiment: Experiment, result: Result) -> "PositiveEvidence":
        if result.verification is not VerificationState.VALID:
            raise CollaborationError("positive evidence requires a verified-valid result")
        if result.experiment_id != experiment.id or result.experiment_hash != experiment.content_hash:
            raise CollaborationError("positive result is not bound to the experiment")
        if result.challenge_hash != experiment.challenge_hash:
            raise CollaborationError("positive result challenge does not match experiment")
        return cls(
            evidence_id=result.content_hash,
            experiment_id=experiment.id,
            experiment_hash=experiment.content_hash,
            result_hash=result.content_hash,
            challenge_hash=experiment.challenge_hash,
            actor=experiment.actor,
            parent_id=experiment.parent_id,
            reproduces_id=experiment.reproduces_id,
        )


class NegativeKnowledge(FrozenModel):
    schema_version: str = "arena.negative.v1"
    evidence_id: str
    experiment_id: str
    challenge_hash: str
    kind: NegativeKind
    reason_codes: tuple[str, ...]
    summary: str
    changed_dimensions: tuple[str, ...] = ()
    artifact_digests: tuple[str, ...] = ()
    created_at: datetime

    @property
    def content_hash(self) -> str:
        return canonical_digest(self)

    @classmethod
    def from_result(
        cls,
        experiment: Experiment,
        result: Result,
        *,
        summary: str,
        created_at: datetime | None = None,
    ) -> "NegativeKnowledge":
        """Create searchable negative evidence bound to verifier/result identity."""

        if result.experiment_id != experiment.id or result.experiment_hash != experiment.content_hash:
            raise CollaborationError("negative result is not bound to the experiment")
        if result.challenge_hash != experiment.challenge_hash:
            raise CollaborationError("negative result challenge does not match experiment")
        kinds = {
            VerificationState.INVALID: NegativeKind.INVALID,
            VerificationState.INCONCLUSIVE: NegativeKind.INCONCLUSIVE,
        }
        if result.verification not in kinds:
            raise CollaborationError("only invalid or inconclusive results are negative evidence")
        reason = result.verification_reason or "verifier_unspecified"
        return cls(
            evidence_id=result.content_hash,
            experiment_id=experiment.id,
            challenge_hash=experiment.challenge_hash,
            kind=kinds[result.verification],
            reason_codes=(reason,),
            summary=summary,
            changed_dimensions=experiment.changed_dimensions,
            artifact_digests=tuple(sorted({item.digest for item in (*experiment.artifacts, *result.artifacts)})),
            created_at=created_at or result.created_at,
        )

    @model_validator(mode="after")
    def validate_searchable_evidence(self) -> "NegativeKnowledge":
        if not self.evidence_id.strip() or not self.experiment_id.strip():
            raise ValueError("negative evidence identity is required")
        if not self.reason_codes or any(not reason.strip() for reason in self.reason_codes):
            raise ValueError("negative evidence requires reason codes")
        if not self.summary.strip():
            raise ValueError("negative evidence requires a summary")
        return self


class NegativeKnowledgeIndex:
    """Rebuildable faceted/full-text index; invalid work remains useful evidence."""

    def __init__(self, records: Iterable[NegativeKnowledge] = ()):
        self._records: dict[str, NegativeKnowledge] = {}
        for record in records:
            previous = self._records.get(record.evidence_id)
            if previous is not None and previous.content_hash != record.content_hash:
                raise CollaborationError(f"conflicting evidence id: {record.evidence_id}")
            self._records[record.evidence_id] = record

    def search(
        self,
        query: str = "",
        *,
        challenge_hash: str | None = None,
        kind: NegativeKind | None = None,
        changed_dimension: str | None = None,
    ) -> tuple[NegativeKnowledge, ...]:
        terms = tuple(re.findall(r"[a-z0-9_.:-]+", query.lower()))
        found = []
        for record in self._records.values():
            if challenge_hash is not None and record.challenge_hash != challenge_hash:
                continue
            if kind is not None and record.kind is not kind:
                continue
            if changed_dimension is not None and changed_dimension not in record.changed_dimensions:
                continue
            haystack = " ".join(
                (record.summary, *record.reason_codes, *record.changed_dimensions)
            ).lower()
            if all(term in haystack for term in terms):
                found.append(record)
        return tuple(sorted(found, key=lambda item: (item.created_at, item.evidence_id)))

    def evidence_ids(self) -> frozenset[str]:
        return frozenset(self._records)


class RefinementScope(str, Enum):
    EXPERIMENT = "experiment"
    PROJECT = "project"
    GLOBAL = "global"


class RefinementState(str, Enum):
    PROPOSED = "proposed"
    CANARY_AUTHORIZED = "canary_authorized"
    CANARY_PASSED = "canary_passed"
    CANARY_FAILED = "canary_failed"
    APPROVED = "approved"
    REJECTED = "rejected"
    PROMOTION_AUTHORIZED = "promotion_authorized"
    PROMOTED = "promoted"
    ROLLBACK_AUTHORIZED = "rollback_authorized"
    ROLLED_BACK = "rolled_back"


class RefinementProposal(FrozenModel):
    schema_version: str = "arena.refinement-proposal.v1"
    id: str
    scope: RefinementScope
    target: str
    proposed_content: str
    evidence_ids: tuple[str, ...]
    proposer: str
    created_at: datetime

    @model_validator(mode="after")
    def validate_proposal(self) -> "RefinementProposal":
        if not self.id.strip() or not self.target.strip() or not self.proposed_content.strip():
            raise ValueError("proposal identity, target and content are required")
        if not self.evidence_ids or any(not item.strip() for item in self.evidence_ids):
            raise ValueError("refinement proposals require evidence")
        return self


class RefinementEvent(FrozenModel):
    schema_version: str = "arena.refinement-event.v1"
    event_id: str
    sequence: int = Field(ge=1)
    proposal_id: str
    proposal_hash: str
    from_state: RefinementState | None
    to_state: RefinementState
    timestamp: datetime
    provenance: Provenance
    evidence_ids: tuple[str, ...]
    receipt: str | None = None
    prior_event_hash: str | None = None
    event_hash: str | None = None

    def calculated_hash(self) -> str:
        return canonical_digest(self.model_dump(mode="json", exclude={"event_hash"}))

    def sealed(self) -> "RefinementEvent":
        digest = self.calculated_hash()
        if self.event_hash is not None and self.event_hash != digest:
            raise CollaborationError("refinement event hash does not match content")
        return self.model_copy(update={"event_hash": digest})


_REFINEMENT_TRANSITIONS: dict[RefinementState | None, set[RefinementState]] = {
    None: {RefinementState.PROPOSED},
    RefinementState.PROPOSED: {
        RefinementState.CANARY_AUTHORIZED,
        RefinementState.REJECTED,
    },
    RefinementState.CANARY_AUTHORIZED: {
        RefinementState.CANARY_PASSED,
        RefinementState.CANARY_FAILED,
    },
    RefinementState.CANARY_PASSED: {RefinementState.APPROVED, RefinementState.REJECTED},
    RefinementState.APPROVED: {RefinementState.PROMOTION_AUTHORIZED},
    RefinementState.PROMOTION_AUTHORIZED: {RefinementState.PROMOTED},
    RefinementState.PROMOTED: {RefinementState.ROLLBACK_AUTHORIZED},
    RefinementState.ROLLBACK_AUTHORIZED: {RefinementState.ROLLED_BACK},
}


class RefinementJournal:
    """Hashed authorization journal with explicit external-operation receipts."""

    def __init__(
        self,
        root: str | Path,
        *,
        approvers: Iterable[str],
        evidence_exists: Callable[[str], bool],
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.proposals_path = self.root / "proposals.jsonl"
        self.events_path = self.root / "events.jsonl"
        self.lock_path = self.root / ".journal.lock"
        self.approvers = frozenset(approvers)
        self._evidence_exists = evidence_exists
        self._thread_lock = threading.RLock()

    def proposals(self) -> dict[str, RefinementProposal]:
        records: dict[str, RefinementProposal] = {}
        if not self.proposals_path.exists():
            return records
        for number, line in enumerate(self.proposals_path.read_bytes().splitlines(), 1):
            try:
                proposal = RefinementProposal.model_validate_json(line)
            except Exception as exc:
                raise CollaborationError(f"invalid refinement proposal at line {number}") from exc
            if proposal.id in records:
                raise CollaborationError(f"duplicate refinement proposal: {proposal.id}")
            records[proposal.id] = proposal
        if self.events_path.exists():
            proposed = {
                event.proposal_id: event.proposal_hash
                for event in self.events()
                if event.from_state is None
                and event.to_state is RefinementState.PROPOSED
            }
            for proposal_id, proposal in records.items():
                expected = proposed.get(proposal_id)
                if expected is not None and expected != canonical_digest(proposal):
                    raise CollaborationError("refinement proposal content was modified")
        return records

    def events(self) -> tuple[RefinementEvent, ...]:
        if not self.events_path.exists():
            return ()
        events: list[RefinementEvent] = []
        prior: str | None = None
        for number, line in enumerate(self.events_path.read_bytes().splitlines(), 1):
            try:
                event = RefinementEvent.model_validate_json(line)
            except Exception as exc:
                raise CollaborationError(f"invalid refinement event at line {number}") from exc
            if event.sequence != number or event.prior_event_hash != prior:
                raise CollaborationError("refinement event sequence/hash chain is corrupt")
            if event.event_hash != event.calculated_hash():
                raise CollaborationError("refinement event seal is invalid")
            events.append(event)
            prior = event.event_hash
        return tuple(events)

    def state(self, proposal_id: str) -> RefinementState | None:
        relevant = [event for event in self.events() if event.proposal_id == proposal_id]
        return relevant[-1].to_state if relevant else None

    def propose(self, proposal: RefinementProposal, provenance: Provenance) -> RefinementEvent:
        with self._locked():
            if any(not self._evidence_exists(item) for item in proposal.evidence_ids):
                raise CollaborationError("proposal cites unknown evidence")
            if proposal.id in self.proposals():
                raise CollaborationError(f"proposal already exists: {proposal.id}")
            self._append_json(
                self.proposals_path, proposal.model_dump_json().encode() + b"\n"
            )
            return self._transition(
                proposal.id,
                RefinementState.PROPOSED,
                provenance,
                proposal.evidence_ids,
                _already_locked=True,
            )

    def authorize_canary(
        self, proposal_id: str, provenance: Provenance, evidence_ids: Iterable[str]
    ) -> RefinementEvent:
        self._require_approver(proposal_id, provenance.actor)
        return self._transition(
            proposal_id, RefinementState.CANARY_AUTHORIZED, provenance, evidence_ids
        )

    def record_canary(
        self,
        proposal_id: str,
        provenance: Provenance,
        *,
        passed: bool,
        evidence_ids: Iterable[str],
        receipt: str,
    ) -> RefinementEvent:
        state = RefinementState.CANARY_PASSED if passed else RefinementState.CANARY_FAILED
        return self._transition(proposal_id, state, provenance, evidence_ids, receipt)

    def approve(
        self, proposal_id: str, provenance: Provenance, evidence_ids: Iterable[str]
    ) -> RefinementEvent:
        self._require_approver(proposal_id, provenance.actor)
        return self._transition(proposal_id, RefinementState.APPROVED, provenance, evidence_ids)

    def reject(
        self, proposal_id: str, provenance: Provenance, evidence_ids: Iterable[str]
    ) -> RefinementEvent:
        self._require_approver(proposal_id, provenance.actor)
        return self._transition(proposal_id, RefinementState.REJECTED, provenance, evidence_ids)

    def authorize_promotion(
        self, proposal_id: str, provenance: Provenance, evidence_ids: Iterable[str]
    ) -> RefinementEvent:
        self._require_approver(proposal_id, provenance.actor)
        return self._transition(
            proposal_id, RefinementState.PROMOTION_AUTHORIZED, provenance, evidence_ids
        )

    def record_promoted(
        self,
        proposal_id: str,
        provenance: Provenance,
        *,
        evidence_ids: Iterable[str],
        receipt: str,
    ) -> RefinementEvent:
        return self._transition(
            proposal_id, RefinementState.PROMOTED, provenance, evidence_ids, receipt
        )

    def authorize_rollback(
        self, proposal_id: str, provenance: Provenance, evidence_ids: Iterable[str]
    ) -> RefinementEvent:
        self._require_approver(proposal_id, provenance.actor)
        return self._transition(
            proposal_id, RefinementState.ROLLBACK_AUTHORIZED, provenance, evidence_ids
        )

    def record_rolled_back(
        self,
        proposal_id: str,
        provenance: Provenance,
        *,
        evidence_ids: Iterable[str],
        receipt: str,
    ) -> RefinementEvent:
        return self._transition(
            proposal_id, RefinementState.ROLLED_BACK, provenance, evidence_ids, receipt
        )

    def _require_approver(self, proposal_id: str, actor: str) -> None:
        proposal = self.proposals().get(proposal_id)
        if proposal is None:
            raise CollaborationError(f"unknown proposal: {proposal_id}")
        if actor not in self.approvers or actor == proposal.proposer:
            raise CollaborationError("independent authorized approver required")

    def _transition(
        self,
        proposal_id: str,
        to_state: RefinementState,
        provenance: Provenance,
        evidence_ids: Iterable[str],
        receipt: str | None = None,
        *,
        _already_locked: bool = False,
    ) -> RefinementEvent:
        lock = nullcontext() if _already_locked else self._locked()
        with lock:
            proposals = self.proposals()
            if proposal_id not in proposals:
                raise CollaborationError(f"unknown proposal: {proposal_id}")
            evidence = tuple(sorted(set(evidence_ids)))
            if not evidence or any(not self._evidence_exists(item) for item in evidence):
                raise CollaborationError("transition requires known evidence")
            if to_state in {
                RefinementState.CANARY_PASSED,
                RefinementState.CANARY_FAILED,
                RefinementState.PROMOTED,
                RefinementState.ROLLED_BACK,
            } and not receipt:
                raise CollaborationError("external state transitions require a receipt")
            events = self.events()
            relevant = [event for event in events if event.proposal_id == proposal_id]
            current = relevant[-1].to_state if relevant else None
            if to_state not in _REFINEMENT_TRANSITIONS.get(current, set()):
                raise CollaborationError(
                    f"illegal refinement transition {current!r} -> {to_state.value}"
                )
            event = RefinementEvent(
                event_id=uuid.uuid4().hex,
                sequence=len(events) + 1,
                proposal_id=proposal_id,
                proposal_hash=canonical_digest(proposals[proposal_id]),
                from_state=current,
                to_state=to_state,
                timestamp=datetime.now(timezone.utc),
                provenance=provenance,
                evidence_ids=evidence,
                receipt=receipt,
                prior_event_hash=events[-1].event_hash if events else None,
            ).sealed()
            self._append_json(
                self.events_path, event.model_dump_json().encode() + b"\n"
            )
            return event

    @contextmanager
    def _locked(self):
        """Serialize the read-validate-append transaction across threads/processes."""

        with self._thread_lock, self.lock_path.open("a+b") as lock:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            yield

    @staticmethod
    def _append_json(path: Path, payload: bytes) -> None:
        with path.open("a+b") as stream:
            if fcntl is not None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            stream.seek(0, os.SEEK_END)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())


def evidence_id(payload: bytes) -> str:
    """Return a stable evidence ID suitable for proposal and receipt references."""

    return "sha256:" + hashlib.sha256(payload).hexdigest()
