from __future__ import annotations

from datetime import datetime, timezone

import pytest

from skharness.arena import (
    CollaborationError,
    ExperimentCatalog,
    NegativeKind,
    NegativeKnowledge,
    NegativeKnowledgeIndex,
    RefinementJournal,
    RefinementProposal,
    RefinementScope,
    RefinementState,
    evidence_id,
)
from skharness.arena.models import (
    BudgetSpec,
    Experiment,
    Provenance,
    Result,
    VerificationState,
)

NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


def experiment(identifier: str = "base") -> Experiment:
    return Experiment(
        id=identifier,
        challenge_hash="sha256:challenge",
        actor="agent:worker",
        harness="pi",
        card_id="card-test",
        run_id=f"run-{identifier}",
        repository_url="https://example.invalid/repo.git",
        repository_base_sha="a" * 40,
        repository_result_sha="b" * 40,
        image_digest="sha256:image",
        sbom_digest="sha256:sbom",
        requested_route="skgw/build",
        requested_model="build",
        served_model="qwen",
        gateway_request_id="gw-1",
        gateway_backend_id="backend-1",
        configuration={"batch": 1},
        hardware_telemetry={"gpu": "test"},
        budgets=BudgetSpec(wall_seconds=60),
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


def test_discovery_reproduction_and_mutation_reset_runtime_evidence():
    base = experiment()
    catalog = ExperimentCatalog((base,))
    assert catalog.discover(challenge_hash="sha256:challenge")[0].experiment == base

    reproduction = catalog.reproduce(
        "base",
        experiment_id="repro",
        actor="agent:two",
        run_id="run-repro",
        created_at=NOW,
    )
    assert reproduction.reproduces_id == "base"
    assert reproduction.parent_id is None
    assert reproduction.served_model is None
    assert reproduction.gateway_request_id is None
    assert reproduction.repository_result_sha is None

    mutation = catalog.mutate(
        "base",
        experiment_id="mutant",
        actor="agent:three",
        run_id="run-mutant",
        changed_dimensions=("configuration.batch",),
        configuration={"batch": 2},
        created_at=NOW,
    )
    assert mutation.parent_id == "base"
    assert mutation.changed_dimensions == ("configuration.batch",)
    assert mutation.configuration == {"batch": 2}
    with pytest.raises(CollaborationError, match="must differ"):
        catalog.mutate(
            "base",
            experiment_id="noop",
            actor="agent:three",
            run_id="run-noop",
            changed_dimensions=("configuration.batch",),
            configuration={"batch": 1},
            created_at=NOW,
        )


def test_negative_results_are_searchable_by_reason_and_changed_dimension():
    first = NegativeKnowledge(
        evidence_id="ev-oom",
        experiment_id="mutant",
        challenge_hash="sha256:challenge",
        kind=NegativeKind.INVALID,
        reason_codes=("quality_floor_failed", "oom"),
        summary="Larger batch exceeded VRAM and missed the quality floor",
        changed_dimensions=("configuration.batch",),
        created_at=NOW,
    )
    second = first.model_copy(
        update={
            "evidence_id": "ev-cache",
            "experiment_id": "cache",
            "reason_codes": ("cache_undisclosed",),
            "summary": "Undisclosed prompt cache",
            "changed_dimensions": ("engine.cache",),
        }
    )
    index = NegativeKnowledgeIndex((first, second))
    assert index.search("VRAM quality", changed_dimension="configuration.batch") == (first,)
    assert index.search("cache", kind=NegativeKind.INVALID) == (second,)


def test_negative_evidence_is_bound_to_an_invalid_verifier_result():
    base = experiment()
    result = Result(
        experiment_id=base.id,
        experiment_hash=base.content_hash,
        challenge_hash=base.challenge_hash,
        verification=VerificationState.INVALID,
        verification_reason="quality_floor_failed",
        measurements=(),
        created_at=NOW,
    )
    negative = NegativeKnowledge.from_result(base, result, summary="Quality regressed")
    assert negative.evidence_id == result.content_hash
    assert negative.reason_codes == ("quality_floor_failed",)

    with pytest.raises(CollaborationError, match="only invalid or inconclusive"):
        NegativeKnowledge.from_result(
            base,
            result.model_copy(update={"verification": VerificationState.VALID}),
            summary="not negative",
        )


def test_refinement_is_fail_closed_and_rollback_is_evidence_linked(tmp_path):
    evidence = {
        evidence_id(b"verified experiment"),
        evidence_id(b"canary report"),
        evidence_id(b"approval"),
        evidence_id(b"promotion receipt"),
        evidence_id(b"incident"),
        evidence_id(b"rollback receipt"),
    }
    journal = RefinementJournal(
        tmp_path, approvers={"operator:casey"}, evidence_exists=evidence.__contains__
    )
    proposal = RefinementProposal(
        id="refine-1",
        scope=RefinementScope.PROJECT,
        target="skmemory:project:skharness",
        proposed_content="Prefer configuration batch=2 for this hardware class.",
        evidence_ids=(evidence_id(b"verified experiment"),),
        proposer="agent:worker",
        created_at=NOW,
    )
    journal.propose(proposal, provenance("agent:worker", "refinement.propose"))

    with pytest.raises(CollaborationError, match="independent authorized approver"):
        journal.authorize_canary(
            proposal.id,
            provenance("agent:worker", "refinement.canary.authorize"),
            proposal.evidence_ids,
        )
    with pytest.raises(CollaborationError, match="illegal refinement transition"):
        journal.authorize_promotion(
            proposal.id,
            provenance("operator:casey", "refinement.promote.authorize"),
            proposal.evidence_ids,
        )

    journal.authorize_canary(
        proposal.id,
        provenance("operator:casey", "refinement.canary.authorize"),
        proposal.evidence_ids,
    )
    with pytest.raises(CollaborationError, match="receipt"):
        journal.record_canary(
            proposal.id,
            provenance("service:canary", "refinement.canary.result"),
            passed=True,
            evidence_ids=(evidence_id(b"canary report"),),
            receipt="",
        )
    journal.record_canary(
        proposal.id,
        provenance("service:canary", "refinement.canary.result"),
        passed=True,
        evidence_ids=(evidence_id(b"canary report"),),
        receipt="canary:42",
    )
    journal.approve(
        proposal.id,
        provenance("operator:casey", "refinement.approve"),
        (evidence_id(b"approval"),),
    )
    journal.authorize_promotion(
        proposal.id,
        provenance("operator:casey", "refinement.promote.authorize"),
        (evidence_id(b"approval"),),
    )
    journal.record_promoted(
        proposal.id,
        provenance("service:skmemory", "refinement.promote.result"),
        evidence_ids=(evidence_id(b"promotion receipt"),),
        receipt="skmemory:version:2",
    )
    journal.authorize_rollback(
        proposal.id,
        provenance("operator:casey", "refinement.rollback.authorize"),
        (evidence_id(b"incident"),),
    )
    journal.record_rolled_back(
        proposal.id,
        provenance("service:skmemory", "refinement.rollback.result"),
        evidence_ids=(evidence_id(b"rollback receipt"),),
        receipt="skmemory:version:1",
    )

    assert journal.state(proposal.id) is RefinementState.ROLLED_BACK
    events = journal.events()
    assert len(events) == 8
    assert events[-1].prior_event_hash == events[-2].event_hash

    proposals_path = tmp_path / "proposals.jsonl"
    proposals_path.write_text(
        proposals_path.read_text().replace("Prefer configuration", "Silently replace")
    )
    with pytest.raises(CollaborationError, match="proposal content was modified"):
        journal.proposals()


def test_unknown_evidence_and_corrupt_journal_fail_closed(tmp_path):
    journal = RefinementJournal(
        tmp_path, approvers={"operator:casey"}, evidence_exists=lambda _: False
    )
    proposal = RefinementProposal(
        id="refine-unknown",
        scope=RefinementScope.GLOBAL,
        target="skmemory:global",
        proposed_content="Never auto-promote this",
        evidence_ids=("missing",),
        proposer="agent:worker",
        created_at=NOW,
    )
    with pytest.raises(CollaborationError, match="unknown evidence"):
        journal.propose(proposal, provenance("agent:worker", "refinement.propose"))

    path = tmp_path / "events.jsonl"
    path.write_text("not-json\n")
    with pytest.raises(CollaborationError, match="invalid refinement event"):
        journal.events()
