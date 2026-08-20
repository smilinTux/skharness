import json
from datetime import datetime, timezone

import pytest

from skharness.arena import CollaborationAccess
from skharness.arena.models import BudgetSpec, Experiment, Result, VerificationState
from skharness.arena.pi_bridge import PI_PROFILES
from skharness.arena.scheduler import AttemptRequest, LeaseScheduler, ResourceRequest
from skharness.arena.sk_backend import BACKEND_OPERATIONS, BackendInputError, LocalSKBackend


def backend(tmp_path):
    return LocalSKBackend(capstone_home=tmp_path / "capstone", event_dir=tmp_path / "events",
                          agent="test-agent")


def test_result_append_is_idempotent_and_collision_safe(tmp_path):
    b = backend(tmp_path)
    payload = {"idempotency_key": "run-1", "record": {"score": 42}}
    assert b.invoke("arena.result.append", payload)["duplicate"] is False
    assert b.invoke("arena.result.append", payload)["duplicate"] is True
    with pytest.raises(BackendInputError, match="collision"):
        b.invoke("arena.result.append", {**payload, "record": {"score": 99}})
    assert json.loads((tmp_path / "events/run-1.json").read_text()) == {"score": 42}


def test_backend_has_exact_schemas_and_no_arbitrary_operation(tmp_path):
    b = backend(tmp_path)
    with pytest.raises(BackendInputError, match="payload keys"):
        b.invoke("arena.result.append", {"idempotency_key": "x", "record": {}, "shell": "id"})
    with pytest.raises(BackendInputError, match="unsupported"):
        b.invoke("mcp.call_anything", {})


def test_collaboration_mutations_fail_closed_without_live_lease_authority(tmp_path):
    b = backend(tmp_path)
    with pytest.raises(BackendInputError, match="lease authority"):
        b.invoke(
            "arena.experiment.reproduce",
            {
                "immutable_evidence_id": "sha256:evidence",
                "experiment_id": "new",
                "attempt_id": "1",
                "run_id": "run-new",
                "created_at": "2026-08-20T00:00:00+00:00",
                "idempotency_key": "new-1",
            },
        )


def test_collaboration_search_operations_return_empty_typed_views(tmp_path):
    b = backend(tmp_path)
    assert b.invoke("arena.experiment.search", {}) == {"experiments": []}
    assert b.invoke("arena.negative.search", {"query": "oom"}) == {
        "negative_evidence": []
    }


def test_backend_reproduces_verified_evidence_under_injected_live_lease(tmp_path):
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    source = Experiment(
        id="source", challenge_hash="sha256:challenge", actor="source-agent", harness="pi",
        run_id="run-source", repository_base_sha="a" * 40,
        image_digest="sha256:image", sbom_digest="sha256:sbom",
        requested_route="skgw/build", requested_model="build",
        budgets=BudgetSpec(wall_seconds=60), created_at=now,
    )
    verified = Result(
        experiment_id=source.id, experiment_hash=source.content_hash,
        challenge_hash=source.challenge_hash, verification=VerificationState.VALID,
        measurements=(), created_at=now,
    )
    scheduler = LeaseScheduler(ResourceRequest(cpu=2))
    admission = scheduler.admit(AttemptRequest("challenge", "copy", "1", "copy-1"))
    assert admission.lease is not None
    access = CollaborationAccess(scheduler)
    access.bind(admission.lease, owner="test-agent")
    b = LocalSKBackend(
        capstone_home=tmp_path / "capstone", event_dir=tmp_path / "events",
        agent="test-agent", collaboration_access=access,
    )
    b.invoke("arena.result.append", {
        "idempotency_key": "source", "record": source.model_dump(mode="json")})
    b.invoke("arena.result.append", {
        "idempotency_key": "verified", "record": verified.model_dump(mode="json")})
    response = b.invoke("arena.experiment.reproduce", {
        "immutable_evidence_id": verified.content_hash, "experiment_id": "copy",
        "attempt_id": "1", "run_id": "run-copy", "created_at": now.isoformat(),
        "idempotency_key": "copy",
    })
    assert response["duplicate"] is False
    copied = json.loads((tmp_path / "events/copy.json").read_text())
    assert copied["reproduces_id"] == source.id
    assert copied["actor"] == "test-agent"


def test_profile_to_backend_operation_schema_parity():
    granted = set().union(*(profile.sk_operations for profile in PI_PROFILES.values()))
    assert granted == BACKEND_OPERATIONS


def test_card_read_uses_real_board_contract(tmp_path):
    b = backend(tmp_path)
    assert b.invoke("capstone.card.read", {"card_id": "missing"}) == {
        "found": False, "card": None}
