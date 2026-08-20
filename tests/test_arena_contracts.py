from __future__ import annotations

import hashlib
import threading
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from skharness.arena import (
    ArenaStore,
    ChallengeSpec,
    CorruptEventLogError,
    EventConflictError,
    Experiment,
    ExperimentEvent,
    ExperimentState,
    LineageGraph,
    Result,
    VerificationState,
)
from skharness.arena.models import (
    BudgetSpec,
    DatasetRef,
    HardwareSpec,
    MetricSpec,
    ModelRef,
    Provenance,
)

NOW = datetime(2026, 8, 20, tzinfo=timezone.utc)


def challenge(**updates) -> ChallengeSpec:
    values = {
        "id": "tiny-python",
        "version": "1",
        "title": "Tiny challenge",
        "owner": "test",
        "created_at": NOW,
        "repository_url": "https://example.invalid/repo.git",
        "base_commit": "a" * 40,
        "writable_paths": ("src",),
        "protected_paths": ("tests/hidden",),
        "task_template": "make it faster",
        "model": ModelRef(model_id="qwen", digest="sha256:model"),
        "hardware": HardwareSpec(hardware_class="cpu", gpu_count=0, image_digest="sha256:image"),
        "public_dataset": DatasetRef(uri="file:public", digest="sha256:public"),
        "withheld_dataset_ref": DatasetRef(uri="vault:hidden", digest="sha256:hidden"),
        "metrics": (MetricSpec(name="latency", unit="ms", objective="minimize"),),
        "confidence_rule": "95% bootstrap CI",
        "budgets": BudgetSpec(wall_seconds=60),
        "verifier_version": "1",
        "rubric_version": "1",
        "policy_version": "1",
        "promotion_requirements": ("valid",),
        "rollback_requirements": ("retain prior",),
    }
    values.update(updates)
    return ChallengeSpec(**values)


def experiment(identifier: str, parent_id: str | None = None) -> Experiment:
    return Experiment(
        id=identifier,
        parent_id=parent_id,
        changed_dimensions=("configuration.batch",) if parent_id else (),
        challenge_hash=challenge().content_hash,
        actor="agent:test",
        harness="pi",
        card_id="card-test",
        run_id=f"run-{identifier}",
        repository_url="https://example.invalid/repo.git",
        repository_base_sha="a" * 40,
        image_digest="sha256:image",
        sbom_digest="sha256:sbom",
        requested_route="skgw/build",
        requested_model="build",
        configuration={"batch": 1},
        budgets=BudgetSpec(wall_seconds=60),
        created_at=NOW,
    )


def event(
    sequence: int,
    from_state: ExperimentState | None,
    to_state: ExperimentState,
    prior: str | None = None,
) -> ExperimentEvent:
    return ExperimentEvent(
        event_id=f"event-{sequence}",
        writer_id="worker-1",
        sequence=sequence,
        experiment_id="exp-1",
        from_state=from_state,
        to_state=to_state,
        timestamp=NOW,
        provenance=Provenance(
            actor="agent:test",
            node="node-1",
            session_id="session-1",
            action="transition",
            target="exp-1",
            observed_prior_state=from_state.value if from_state else None,
        ),
        prior_event_hash=prior,
    )


def test_challenge_is_immutable_and_hash_is_canonical():
    first = challenge()
    second = challenge()
    assert first.content_hash == second.content_hash
    with pytest.raises(ValidationError):
        first.title = "changed"
    assert challenge(version="2").content_hash != first.content_hash


@pytest.mark.parametrize(
    ("model_type", "payload"),
    [
        (ChallengeSpec, lambda: challenge().model_dump()),
        (Experiment, lambda: experiment("unknown-version").model_dump()),
        (
            Result,
            lambda: Result(
                experiment_id="exp",
                experiment_hash="sha256:experiment",
                challenge_hash=challenge().content_hash,
                verification=VerificationState.UNVERIFIED,
                measurements=(),
                created_at=NOW,
            ).model_dump(),
        ),
        (
            ExperimentEvent,
            lambda: event(1, None, ExperimentState.PROPOSED).model_dump(),
        ),
    ],
)
def test_unknown_contract_versions_fail_closed(model_type, payload):
    document = payload()
    document["schema_version"] = "arena.unknown.v999"
    with pytest.raises(ValidationError, match="schema_version"):
        model_type.model_validate(document)


def test_experiment_contract_carries_complete_attribution_and_execution_provenance():
    item = experiment("provenance").model_copy(
        update={
            "repository_result_sha": "b" * 40,
            "served_model": "qwen3.8",
            "gateway_request_id": "request-1",
            "gateway_backend_id": "backend-1",
            "seeds": (7, 11),
            "hardware_telemetry": {"node": ".41", "gpu": "RTX"},
        }
    )
    document = item.model_dump(mode="json")
    assert {
        "actor",
        "card_id",
        "run_id",
        "repository_url",
        "repository_base_sha",
        "repository_result_sha",
        "image_digest",
        "sbom_digest",
        "requested_model",
        "served_model",
        "gateway_request_id",
        "gateway_backend_id",
        "hardware_telemetry",
        "seeds",
        "budgets",
        "artifacts",
    } <= document.keys()
    assert Provenance.model_fields.keys() >= {
        "actor",
        "node",
        "session_id",
        "action",
        "target",
        "observed_prior_state",
        "signature",
    }


def test_challenge_rejects_overlapping_paths_and_metrics():
    with pytest.raises(ValidationError, match="must not overlap"):
        challenge(writable_paths=("src",), protected_paths=("src",))
    duplicate = MetricSpec(name="latency", unit="ms", objective="minimize")
    with pytest.raises(ValidationError, match="uniquely named"):
        challenge(metrics=(duplicate, duplicate))


def test_event_log_seals_hash_chain_and_rejects_stale_append(tmp_path):
    store = ArenaStore(tmp_path)
    first = store.append_event(event(1, None, ExperimentState.PROPOSED))
    second = store.append_event(
        event(2, ExperimentState.PROPOSED, ExperimentState.ADMITTED, first.event_hash)
    )
    assert store.read_segment("worker-1") == [first, second]
    with pytest.raises(EventConflictError):
        store.append_event(event(2, ExperimentState.PROPOSED, ExperimentState.ADMITTED))


def test_concurrent_writers_cannot_commit_the_same_observed_head(tmp_path):
    store = ArenaStore(tmp_path)
    barrier = threading.Barrier(2)
    committed = []
    conflicts = []

    def append() -> None:
        barrier.wait()
        try:
            committed.append(store.append_event(event(1, None, ExperimentState.PROPOSED)))
        except EventConflictError as exc:
            conflicts.append(exc)

    writers = [threading.Thread(target=append) for _ in range(2)]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join()
    assert len(committed) == 1
    assert len(conflicts) == 1
    assert store.read_segment("worker-1") == committed


def test_incomplete_tail_is_ignored_then_truncated_on_append(tmp_path):
    store = ArenaStore(tmp_path)
    first = store.append_event(event(1, None, ExperimentState.PROPOSED))
    segment = tmp_path / "events" / "worker-1.jsonl"
    with segment.open("ab") as stream:
        stream.write(b'{"partial":')
    assert store.read_segment("worker-1") == [first]
    second = store.append_event(
        event(2, ExperimentState.PROPOSED, ExperimentState.ADMITTED, first.event_hash)
    )
    assert store.read_segment("worker-1") == [first, second]


def test_committed_corruption_fails_closed(tmp_path):
    store = ArenaStore(tmp_path)
    store.append_event(event(1, None, ExperimentState.PROPOSED))
    segment = tmp_path / "events" / "worker-1.jsonl"
    with segment.open("ab") as stream:
        stream.write(b"not-json\n")
    with pytest.raises(CorruptEventLogError):
        store.read_segment("worker-1")


def test_illegal_state_transition_is_rejected():
    with pytest.raises(ValidationError, match="illegal experiment transition"):
        event(1, None, ExperimentState.VALID)


def test_artifacts_are_content_addressed_and_verified(tmp_path):
    store = ArenaStore(tmp_path)
    content = b"raw benchmark evidence"
    digest = store.put_artifact(content)
    assert digest == "sha256:" + hashlib.sha256(content).hexdigest()
    assert store.put_artifact(content) == digest
    assert store.get_artifact(digest) == content


def test_lineage_reports_ancestry_and_rejects_conflicting_identity():
    root = experiment("root")
    child = experiment("child", "root")
    grandchild = experiment("grandchild", "child")
    graph = LineageGraph((root, child, grandchild))
    assert [item.id for item in graph.ancestors("grandchild")] == ["child", "root"]
    assert [item.id for item in graph.descendants("root")] == ["child", "grandchild"]
    with pytest.raises(ValueError, match="conflicting content"):
        graph.add(root.model_copy(update={"harness": "other"}))


def test_lineage_rejects_a_cycle_completed_by_a_later_record():
    graph = LineageGraph()
    graph.add(experiment("first", "second"))
    with pytest.raises(ValueError, match="cycle"):
        graph.add(experiment("second", "first"))


def test_multi_writer_event_fold_is_deterministic_regardless_of_append_order(tmp_path):
    def folded(root, writer_order):
        store = ArenaStore(root)
        for writer_id in writer_order:
            store.append_event(
                event(1, None, ExperimentState.PROPOSED).model_copy(
                    update={"event_id": f"event-{writer_id}", "writer_id": writer_id}
                )
            )
        return [
            (item.timestamp, item.writer_id, item.sequence, item.event_hash)
            for item in store.read_all_events()
        ]

    assert folded(tmp_path / "forward", ("worker-a", "worker-b")) == folded(
        tmp_path / "reverse", ("worker-b", "worker-a")
    )
