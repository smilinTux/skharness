from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from skharness.arena import (
    AccessDeniedError,
    CollaborationAccess,
    CollaborationError,
    ExperimentCatalog,
    MetricDirection,
    MetricObjective,
    MetricSummary,
    RefinementJournal,
    RefinementProposal,
    RefinementScope,
    RefinementState,
    RuntimeSKMemoryAdapter,
    VerifiedParetoCandidate,
    verified_pareto_frontier,
)
from skharness.arena.models import (
    BudgetSpec,
    Experiment,
    Provenance,
    Result,
    VerificationState,
)
from skharness.arena.scheduler import AttemptRequest, LeaseScheduler, ResourceRequest

NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


def experiment(identifier: str = "base") -> Experiment:
    return Experiment(
        id=identifier,
        challenge_hash="sha256:challenge",
        actor="agent:one",
        harness="pi",
        run_id=f"run-{identifier}",
        repository_base_sha="a" * 40,
        image_digest="sha256:image",
        sbom_digest="sha256:sbom",
        requested_route="skgw/build",
        requested_model="build",
        configuration={"batch": 1},
        budgets=BudgetSpec(wall_seconds=60),
        created_at=NOW,
    )


def result(item: Experiment, state: VerificationState) -> Result:
    return Result(
        experiment_id=item.id,
        experiment_hash=item.content_hash,
        challenge_hash=item.challenge_hash,
        verification=state,
        verification_reason=None if state is VerificationState.VALID else "failed",
        measurements=(),
        created_at=NOW,
    )


def provenance(actor: str, action: str) -> Provenance:
    return Provenance(
        actor=actor,
        node="node-1",
        session_id="session-1",
        action=action,
        target="memory:project:test",
    )


def summary(value: float) -> MetricSummary:
    return MetricSummary(2, value, 0, value, value, value, value)


def test_positive_evidence_id_is_valid_only_and_reproduces_with_credit():
    valid = experiment("valid")
    invalid = experiment("invalid")
    valid_result = result(valid, VerificationState.VALID)
    catalog = ExperimentCatalog(
        (valid, invalid), (valid_result, result(invalid, VerificationState.INVALID))
    )
    evidence = catalog.positive_evidence()
    assert len(evidence) == 1
    assert evidence[0].evidence_id == valid_result.content_hash
    reproduction = catalog.reproduce_evidence(
        evidence[0].evidence_id,
        experiment_id="reproduction",
        actor="agent:two",
        run_id="run-reproduction",
        created_at=NOW,
    )
    assert reproduction.reproduces_id == valid.id
    with pytest.raises(CollaborationError, match="non-valid"):
        catalog.reproduce_evidence(
            result(invalid, VerificationState.INVALID).content_hash,
            experiment_id="bad",
            actor="agent:two",
            run_id="run-bad",
            created_at=NOW,
        )


def test_frontier_admission_rejects_invalid_result_before_scoring():
    valid = experiment("valid")
    invalid = experiment("invalid")
    metrics = {"tps": summary(100)}
    admitted = VerifiedParetoCandidate.from_result(
        result(valid, VerificationState.VALID), metrics
    )
    with pytest.raises(ValueError, match="verified-valid"):
        VerifiedParetoCandidate.from_result(
            result(invalid, VerificationState.INVALID), {"tps": summary(1_000_000)}
        )
    assert verified_pareto_frontier(
        (admitted,), (MetricObjective("tps", MetricDirection.MAXIMIZE),)
    ) == (admitted,)


def test_attempt_ownership_and_a2a_inbox_are_bound_to_live_lease():
    scheduler = LeaseScheduler(ResourceRequest(cpu=2), lease_ttl_s=60)
    admission = scheduler.admit(
        AttemptRequest("challenge", "experiment", "1", "attempt-key")
    )
    assert admission.lease is not None
    access = CollaborationAccess(scheduler)
    access.bind(admission.lease, owner="agent:one")
    assert access.require_owner("experiment", "1", actor="agent:one") is admission.lease
    with pytest.raises(AccessDeniedError, match="does not own"):
        access.require_owner("experiment", "1", actor="agent:two")
    with pytest.raises(AccessDeniedError, match="no A2A grant"):
        access.send(
            sender="agent:one", recipient="agent:two", experiment_id="experiment",
            attempt_id="1", body="reproduce this",
        )
    with pytest.raises(AccessDeniedError, match="only an agent"):
        access.grant_peer(actor="agent:two", owner="agent:one", peer="agent:two")
    access.grant_peer(actor="agent:one", owner="agent:one", peer="agent:two")
    sent = access.send(
        sender="agent:one", recipient="agent:two", experiment_id="experiment",
        attempt_id="1", body="reproduce this",
    )
    assert access.inbox(actor="agent:two") == (sent,)
    assert access.inbox(actor="agent:three") == ()
    scheduler.release(admission.lease.lease_id)
    with pytest.raises(AccessDeniedError, match="no longer active"):
        access.require_owner("experiment", "1", actor="agent:one")


def test_reproduction_workflow_cannot_bypass_attempt_ownership():
    source = experiment("source")
    source_result = result(source, VerificationState.VALID)
    catalog = ExperimentCatalog((source,), (source_result,))
    scheduler = LeaseScheduler(ResourceRequest(cpu=2), lease_ttl_s=60)
    admission = scheduler.admit(
        AttemptRequest("challenge", "new-experiment", "1", "new-attempt")
    )
    assert admission.lease is not None
    access = CollaborationAccess(scheduler)
    access.bind(admission.lease, owner="agent:one")
    with pytest.raises(AccessDeniedError, match="does not own"):
        access.reproduce(
            catalog, source_result.content_hash, experiment_id="new-experiment",
            attempt_id="1", actor="agent:two", run_id="bad", created_at=NOW,
        )
    reproduced = access.reproduce(
        catalog, source_result.content_hash, experiment_id="new-experiment",
        attempt_id="1", actor="agent:one", run_id="good", created_at=NOW,
    )
    assert reproduced.reproduces_id == source.id


class FakeMemory:
    def __init__(self):
        self.values = {"skmemory:project:test": ("sha256:old", "old content")}
        self.versions = {"sha256:old": "old content"}
        self.receipts = {}
        self.calls = []

    def __call__(self, operation, payload):
        self.calls.append((operation, payload))
        target = payload["target"]
        if operation == "memory.read":
            return {"content_hash": self.values[target][0]}
        key = payload["idempotency_key"]
        if key in self.receipts:
            return self.receipts[key]
        current_hash, _ = self.values[target]
        assert current_hash == payload["expected_content_hash"]
        if operation == "memory.compare_and_set":
            content = payload["content"]
            new_hash = "sha256:" + hashlib.sha256(content.encode()).hexdigest()
            self.versions[new_hash] = content
        elif operation == "memory.restore":
            new_hash = payload["restore_content_hash"]
            content = self.versions[new_hash]
        else:
            raise AssertionError(operation)
        self.values[target] = (new_hash, content)
        response = {
            "receipt": f"receipt:{key}",
            "prior_content_hash": current_hash,
            "content_hash": new_hash,
        }
        self.receipts[key] = response
        return response


def ready_journal(tmp_path):
    evidence = {"verified", "canary", "approval", "incident"}
    journal = RefinementJournal(
        tmp_path, approvers={"operator:casey"}, evidence_exists=evidence.__contains__
    )
    proposal = RefinementProposal(
        id="refine-1", scope=RefinementScope.PROJECT,
        target="skmemory:project:test", proposed_content="new content",
        evidence_ids=("verified",), proposer="agent:one", created_at=NOW,
    )
    journal.propose(proposal, provenance("agent:one", "propose"))
    journal.authorize_canary(
        proposal.id, provenance("operator:casey", "canary.authorize"), ("verified",)
    )
    journal.record_canary(
        proposal.id, provenance("service:canary", "canary.result"), passed=True,
        evidence_ids=("canary",), receipt="canary:passed",
    )
    journal.approve(
        proposal.id, provenance("operator:casey", "approve"), ("approval",)
    )
    journal.authorize_promotion(
        proposal.id, provenance("operator:casey", "promote.authorize"), ("approval",)
    )
    return journal, proposal


def test_runtime_skmemory_adapter_captures_and_restores_prior_hash_idempotently(tmp_path):
    journal, proposal = ready_journal(tmp_path)
    backend = FakeMemory()
    adapter = RuntimeSKMemoryAdapter(journal, backend)
    promoted = adapter.promote(proposal.id, provenance("service:skmemory", "promote"))
    assert promoted.prior_content_hash == "sha256:old"
    assert adapter.promote(proposal.id, provenance("service:skmemory", "promote")) == promoted
    journal.authorize_rollback(
        proposal.id, provenance("operator:casey", "rollback.authorize"), ("incident",)
    )
    rolled_back = adapter.rollback(
        proposal.id, provenance("service:skmemory", "rollback")
    )
    assert rolled_back.resulting_content_hash == "sha256:old"
    assert adapter.rollback(
        proposal.id, provenance("service:skmemory", "rollback")
    ) == rolled_back
    assert backend.values[proposal.target][0] == promoted.prior_content_hash
    assert [name for name, _ in backend.calls].count("memory.compare_and_set") == 1
    assert [name for name, _ in backend.calls].count("memory.restore") == 1


def test_refinement_journal_serializes_concurrent_proposals_and_transition_race(tmp_path):
    evidence = {f"e-{index}" for index in range(20)}
    journal = RefinementJournal(
        tmp_path, approvers={"operator:casey"}, evidence_exists=evidence.__contains__
    )
    peer_journal = RefinementJournal(
        tmp_path, approvers={"operator:casey"}, evidence_exists=evidence.__contains__
    )

    def propose(index):
        proposal = RefinementProposal(
            id=f"p-{index}", scope=RefinementScope.PROJECT, target=f"memory:{index}",
            proposed_content=f"content {index}", evidence_ids=(f"e-{index}",),
            proposer=f"agent:{index}", created_at=NOW,
        )
        writer = journal if index % 2 else peer_journal
        return writer.propose(proposal, provenance(f"agent:{index}", "propose"))

    with ThreadPoolExecutor(max_workers=8) as pool:
        events = tuple(pool.map(propose, range(20)))
    assert len(events) == 20
    assert [event.sequence for event in journal.events()] == list(range(1, 21))

    def authorize():
        return journal.authorize_canary(
            "p-0", provenance("operator:casey", "canary.authorize"), ("e-0",)
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(authorize) for _ in range(2)]
    outcomes = []
    for future in futures:
        try:
            outcomes.append(future.result())
        except CollaborationError:
            pass
    assert len(outcomes) == 1
    assert journal.state("p-0") is RefinementState.CANARY_AUTHORIZED
    assert [event.sequence for event in journal.events()] == list(range(1, 22))
